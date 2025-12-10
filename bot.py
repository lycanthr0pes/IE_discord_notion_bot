import os
import re
import json
import requests
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta

# ==============================
# 環境変数
# ==============================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")

# Q&A 用（質問 / 回答 / 質問番号）
NOTION_QA_DB_ID = os.getenv("NOTION_QA_ID")

# イベント用（イベント名 / 内容 / 日時 / メッセージID / 作成者ID / ページID）
NOTION_EVENT_DB_ID = os.getenv("NOTION_EVENT_ID")

# チャンネル紐付け
EVENT_CHANNEL_ID = int(os.getenv("EVENT_CHANNEL_ID", 0))
QA_CHANNEL_ID = int(os.getenv("QA_CHANNEL_ID", 0))

# ==============================
# 共通 Notion ヘッダ
# ==============================
headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

JST = timezone(timedelta(hours=9))

# 起動直後にQ&A通知をスキップするためのフラグ
FIRST_QA_RUN = True


# ==============================
# チャンネル制限
# ==============================
def is_event_channel(interaction: discord.Interaction) -> bool:
    return EVENT_CHANNEL_ID == 0 or interaction.channel_id == EVENT_CHANNEL_ID


def is_qa_channel(interaction: discord.Interaction) -> bool:
    return QA_CHANNEL_ID == 0 or interaction.channel_id == QA_CHANNEL_ID


# ==============================
# 日付フォーマット
# ==============================
# ISO形式の日付フォーマットを日本式表示に（Discord用）
def format_display_date(date_iso: str) -> str:
    dt = datetime.fromisoformat(date_iso)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    w = weekdays[dt.weekday()]
    try:
        return dt.strftime(f"%#m月%#d日（{w}） %H:%M")  # Windows
    except Exception:
        return dt.strftime(f"%-m月%-d日（{w}） %H:%M")  # Linux/Mac

# 入力された日付と時刻をISO形式に変換（Notion用）
def normalize_date(date_str: str, time_str: str) -> str:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
    dt = dt.replace(tzinfo=JST)
    return dt.isoformat()


# ======================================================
# イベント管理機能
# ======================================================

# イベントをNotionに新規作成（jsonを作成し送信）
def notion_add_event(name, content, date_iso, message_id, creator_id):
    url = "https://api.notion.com/v1/pages"
    data = {
        "parent": {"database_id": NOTION_EVENT_DB_ID},
        "properties": {
            "イベント名": {"title": [{"text": {"content": name}}]},
            "内容": {"rich_text": [{"text": {"content": content}}]},
            "日時": {"date": {"start": date_iso}},
            "メッセージID": {"rich_text": [{"text": {"content": str(message_id)}}]},
            "作成者ID": {"rich_text": [{"text": {"content": str(creator_id)}}]},
            "ページID": {"rich_text": [{"text": {"content": ""}}]},
        },
    }
    res = requests.post(url, headers=headers, json=data)
    if res.status_code not in (200, 201):
        # ログ出力
        print("❌ Notion作成エラー:", res.text)
        return None
    
    # ページIDを追加
    page_id = res.json()["id"]
    notion_update_event(page_id, page_uuid=page_id)
    return page_id

# Notion APIを使ってNotion上のイベントを取得し、JSONデータを返す
def notion_get_event(page_id):
    res = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    data = res.json()
    return data if "id" in data else None

# 指定されたNotionイベントのプロパティを更新する
def notion_update_event(page_id, name=None, content=None, date_iso=None, message_id=None, page_uuid=None):
    props = {}
    if name is not None:
        props["イベント名"] = {"title": [{"text": {"content": name}}]}
    if content is not None:
        props["内容"] = {"rich_text": [{"text": {"content": content}}]}
    if date_iso is not None:
        props["日時"] = {"date": {"start": date_iso}}
    if message_id is not None:
        props["メッセージID"] = {"rich_text": [{"text": {"content": str(message_id)}}]}
    if page_uuid is not None:
        props["ページID"] = {"rich_text": [{"text": {"content": str(page_uuid)}}]}

    res = requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=headers, json={"properties": props})
    return res.status_code in (200, 201)

# イベントをNotionからアーカイブ扱いで削除する
def notion_delete_event(page_id):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {"archived": True}

    res = requests.patch(url, headers=headers, json=data)

    #ログ出力
    if res.status_code not in (200, 201):
        print("❌ Notion削除エラー:", res.text)
        return False

    return True

# 過去(24時間前)のイベントをNotionからアーカイブ扱いで削除する
def delete_past_events():
    # クエリを送って全イベントを取得（json）
    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_DB_ID}/query"
    res = requests.post(url, headers=headers, json={}).json()

    # 日付(日本時間)を取得 
    today = datetime.now(JST).date()

    # jsonから各イベントの日時プロパティを確認
    for page in res.get("results", []):
        date_prop = page["properties"]["日時"]["date"]
        if not date_prop:
            continue

    # 日付(ISO形式)をdatetimeに変換する(startは日時プロパティの開始日時)
        dt = datetime.fromisoformat(date_prop["start"]).date()

        # 今日より前なら削除
        if dt < today:
            requests.patch(
                f"https://api.notion.com/v1/pages/{page['id']}",
                headers=headers,
                json={"archived": True},
            )
            # ログ出力
            print(f"[AUTO DELETE] {page['id']} をアーカイブ（削除）しました ({dt})")

# クエリを送って全イベントを取得（json）
def fetch_event_pages():
    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_DB_ID}/query"
    res = requests.post(url, headers=headers, json={})
    if res.status_code != 200:
        # ログ出力
        print("❌ イベント一覧取得失敗:", res.text)
        return []
    return res.json().get("results", [])


# ==============================
# モーダル（イベント編集・作成）
# ==============================
# イベント編集
class EventEditModal(discord.ui.Modal, title="イベント編集"):
    def __init__(self, page, message_id):
        super().__init__()
        # ページID(Notion)とメッセージID(Discord)を保持
        self.page_id = page["id"]
        self.message_id = message_id

        # Notionのプロパティを取り出す
        props = page["properties"]
        name_val = props["イベント名"]["title"][0]["text"]["content"]
        content_val = props["内容"]["rich_text"][0]["text"]["content"] if props["内容"]["rich_text"] else ""
        
        # 日付(ISO)をdatetimeに
        iso = props["日時"]["date"]["start"]
        dt = datetime.fromisoformat(iso)

        # モーダル項目を生成
        self.name = discord.ui.TextInput(label="イベント名", default=name_val)
        self.date = discord.ui.TextInput(label="日付（例:2025/1/1）", default=f"{dt.year}/{dt.month}/{dt.day}")
        self.time = discord.ui.TextInput(label="時刻（例:13:00）", default=f"{dt.hour:02d}:{dt.minute:02d}")
        self.content = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, default=content_val)

        # 生成した項目を追加
        self.add_item(self.name)
        self.add_item(self.date)
        self.add_item(self.time)
        self.add_item(self.content)
    
    # 送信後の処理
    async def on_submit(self, interaction: discord.Interaction):
        # チャンネル制限のチェック
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ この操作は <#{EVENT_CHANNEL_ID}> のみで可能です。",
                ephemeral=True,
            )

        # 入力された日付をISOにして、Notion側のイベントを更新
        # 入力バリデーション
        try:
            date_iso = normalize_date(...)
        except ValueError:
            return await interaction.response.send_message("❌ 日付または時刻の形式が不正です", ephemeral=True)
        
        ok = notion_update_event(self.page_id, self.name.value, self.content.value, date_iso)
        if not ok:
            return await interaction.response.send_message("❌ Notion 更新に失敗しました", ephemeral=True)

        # Discord側を更新
        embed = discord.Embed(title=f"📌 {self.name.value}", color=0x55FF55)
        embed.add_field(name="日時", value=format_display_date(date_iso), inline=False)
        embed.add_field(name="内容", value=self.content.value, inline=False)

        msg = await interaction.channel.fetch_message(int(self.message_id))
        await msg.edit(embed=embed)

        await interaction.response.send_message("✏️ 更新しました", ephemeral=True)


class EventCreateModal(discord.ui.Modal, title="イベント登録"):
    def __init__(self):
        super().__init__()

        # イベント名
        self.name = discord.ui.TextInput(
            label="イベント名",
            required=True
        )
        self.add_item(self.name)

        # 日付
        self.date = discord.ui.TextInput(
            label="日付（YYYY/M/D）",
            placeholder="例：2025/1/1",
            required=True
        )
        self.add_item(self.date)

        # 時刻
        self.time = discord.ui.TextInput(
            label="時刻（HH:MM）",
            placeholder="例：13:00",
            required=True
        )
        self.add_item(self.time)

        # 内容
        self.content = discord.ui.TextInput(
            label="内容",
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction):
        # 入力取得
        name = self.name.value
        date = self.date.value
        time = self.time.value
        content = self.content.value or "(内容なし)"

        # 入力バリデーション
        if not re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", date):
            return await interaction.response.send_message(
                "❌ 日付形式が不正です（例：2025/1/1）", ephemeral=True
            )

        if not re.match(r"^\d{1,2}:\d{2}$", time):
            return await interaction.response.send_message(
                "❌ 時刻形式が不正です（例：13:00）", ephemeral=True
            )

        date_iso = normalize_date(date, time)

        # Discord メッセージ作成
        embed = discord.Embed(title=f"📌 {name}", color=0x00AAFF)
        embed.add_field(name="日時", value=format_display_date(date_iso), inline=False)
        embed.add_field(name="内容", value=content, inline=False)

        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()

        # Notionに保存
        notion_add_event(name, content, date_iso, msg.id, interaction.user.id)


# ==============================
# イベント選択用プルダウン
# ==============================
class EventSelectView(discord.ui.View):
    def __init__(self, pages, mode: str):
        super().__init__(timeout=120) #120秒タイムアウト
        self.mode = mode  # "modify" or "delete"
        self.page_info = {} #選択したページを保持
        
        # 新しい順に並びかえる
        pages.sort(key=lambda p: p["created_time"], reverse=True)

        options = []
        for page in pages[:25]: #Discordのプルダウンメニューは25個まで
            pid = page["id"]
            name = page["properties"]["イベント名"]["title"][0]["text"]["content"]
            self.page_info[pid] = page
            options.append(discord.SelectOption(label=name, value=pid)) # 各選択肢にイベントのIDを登録
        
        # セレクトメニュー本体
        select = discord.ui.Select(
            placeholder="イベント名を選択してください",
            options=options,
            min_values=1,
            max_values=1,
        )
        # UIにセレクトメニュー組み込み
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        # ページIDを取得
        pid = interaction.data["values"][0]
        page = self.page_info[pid]

        # 作成者 or 管理者のみ操作可能(作成者IDで判定)
        creator_id = page["properties"]["作成者ID"]["rich_text"][0]["text"]["content"]
        is_creator = str(interaction.user.id) == creator_id
        is_admin = interaction.user.guild_permissions.manage_guild
        if not (is_creator or is_admin):
            return await interaction.response.send_message(
                "❌ 編集・削除できるのは作成者本人または管理者のみです。",
                ephemeral=True,
            )

        # Discord メッセージIDを Notion から取り出す
        msg_id = page["properties"]["メッセージID"]["rich_text"][0]["text"]["content"]

        if self.mode == "modify":
            # 編集モーダルを表示
            await interaction.response.send_modal(EventEditModal(page, msg_id))
        elif self.mode == "delete":
            # Discordメッセージ削除
            try:
                message = await interaction.channel.fetch_message(int(msg_id))
                await message.delete()
            except Exception:
                pass

            # Notionページ削除
            notion_delete_event(pid)

            await interaction.response.send_message("🗑️ イベントを削除しました。", ephemeral=True)


# ==============================
# イベント用コマンド
# ==============================
class EventCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    # 登録コマンドを定義
    @app_commands.command(name="event", description="イベントを登録します")
    async def event(self, interaction: discord.Interaction):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{EVENT_CHANNEL_ID}> のみ実行できます。",
                ephemeral=True,
            )

        # 入力欄つきモーダルを表示する
        await interaction.response.send_modal(EventCreateModal())

    # 編集コマンドを定義
    @app_commands.command(name="event_modify", description="イベント名から編集するイベントを選択します")
    async def event_modify(self, interaction: discord.Interaction):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{EVENT_CHANNEL_ID}> でのみ利用できます。",
                ephemeral=True,
            )

        # イベント一覧取得
        pages = fetch_event_pages()
        if not pages:
            return await interaction.response.send_message("イベントがありません。", ephemeral=True)
        
        view = EventSelectView(pages, mode="modify")
        await interaction.response.send_message(
            "編集するイベントを選択してください。",
            view=view,
            ephemeral=True,
        )

    # 削除コマンドを定義
    @app_commands.command(name="event_delete", description="イベント名から削除するイベントを選択します")
    async def event_delete(self, interaction: discord.Interaction):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{EVENT_CHANNEL_ID}> でのみ利用できます。",
                ephemeral=True,
            )
        
        # イベント一覧取得
        pages = fetch_event_pages()
        if not pages:
            return await interaction.response.send_message("イベントがありません。", ephemeral=True)

        view = EventSelectView(pages, mode="delete")
        await interaction.response.send_message(
            "削除するイベントを選択してください。",
            view=view,
            ephemeral=True,
        )


# ======================================================
# Q&A 機能
# ======================================================

# Q&A DBの取得・差分管理
def fetch_qa_db():
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    res = requests.post(url, headers=headers, json={})
    return res.json() if res.status_code == 200 else None

#ローカルにjsonファイル作成
CACHE_FILE = "notion_cache.json"

# キャッシュ読み込み
def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# キャッシュ書き込み＋初回起動フラグ
def save_cache(cache, first_run_flag=None):
    # FIRST_QA_RUN のフラグをキャッシュに保存
    if first_run_flag is not None:
        cache["_first_qa_run"] = first_run_flag

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# 新規 / 更新ページの検出
def get_qa_changes():
    data = fetch_qa_db()
    if not data:
        return []

    cache = load_cache()
    new_cache = {}
    changes = []

    # NotionのページIDと最終編集時刻を比較して返す
    for page in data.get("results", []):
        pid = page["id"]
        last = page["last_edited_time"]
        new_cache[pid] = last
        if pid not in cache:
            changes.append(("new", page))
        elif cache[pid] != last:
            changes.append(("update", page))

    save_cache(new_cache)
    return changes


def get_question(page) -> str:
    t = page["properties"]["質問"]["title"]
    return t[0]["plain_text"] if t else "(質問なし)"


def get_answer(page) -> str:
    t = page["properties"]["回答"]["rich_text"]
    return t[0]["plain_text"] if t else "(回答なし)"

# 未回答の質問一覧
def fetch_unanswered():
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    data = {"filter": {"property": "回答", "rich_text": {"is_empty": True}}}
    res = requests.post(url, headers=headers, json=data)
    # APIリクエストが成功したらjsonを返す
    return res.json().get("results", []) if res.status_code == 200 else []

# 回答済みの質問一覧
def fetch_answered():
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    data = {"filter": {"property": "回答", "rich_text": {"is_not_empty": True}}}
    res = requests.post(url, headers=headers, json=data)
    return res.json().get("results", []) if res.status_code == 200 else []

# Notionに回答を書き込む
def update_answer(page_id, answer: str) -> bool:
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {
        "properties": {
            "回答": {
                "rich_text": [{"type": "text", "text": {"content": answer}}]
            }
        }
    }
    # 更新リクエストと成功判定
    return requests.patch(url, headers=headers, json=data).status_code == 200


async def send_qa_notification(bot: commands.Bot, ctype: str, page: dict):
    # Q&Aの新規/更新通知（未回答のみ対象）
    if QA_CHANNEL_ID == 0:
        return

    ch = await bot.fetch_channel(QA_CHANNEL_ID)

    number = page["properties"]["質問番号"]["number"]
    number_display = number if number is not None else "?"

    q = get_question(page)
    a = get_answer(page)

    if ctype == "new":
        msg = (
            f"🆕 **新しい質問 (#{number_display}) が追加されました！**\n"
            f"**質問:** {q}\n"
            f"**回答:** {a}"
        )
    else:
        msg = (
            f"✏️ **質問 (#{number_display}) が更新されました。**\n"
            f"**質問:** {q}\n"
            f"**回答:** {a}"
        )

    await ch.send(msg)


# ==============================
# Q&A モーダル & 質問選択プルダウン
# ==============================
# 未回答の質問に新規回答を入力するモーダル
class QAnswerModal(discord.ui.Modal):
    #ページID、質問番号、質問文
    def __init__(self, page_id, number, question_text):
        super().__init__(title=f"回答入力（#{number}）")
        self.page_id = page_id
        self.number = number

        self.answer = discord.ui.TextInput(
            label=f"質問: {question_text}",
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        ok = update_answer(self.page_id, self.answer.value)
        if ok:
            await interaction.response.send_message(
                f"✅ 回答を保存しました。（#{self.number}）",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ 回答の保存に失敗しました。",
                ephemeral=True,
            )


class QEditModal(discord.ui.Modal):
    # 回答済みの質問について、回答を編集するモーダル

    def __init__(self, page_id, number, question_text, current_answer):
        super().__init__(title=f"回答編集（#{number}）")
        self.page_id = page_id
        self.number = number

        self.answer = discord.ui.TextInput(
            label=f"質問: {question_text}",
            style=discord.TextStyle.paragraph,
            default=current_answer,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        ok = update_answer(self.page_id, self.answer.value)
        if ok:
            await interaction.response.send_message(
                f"✅ 回答を更新しました。（#{self.number}）",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ 回答の更新に失敗しました。",
                ephemeral=True,
            )


class AnswerSelectView(discord.ui.View):
    # 未回答質問用の番号選択プルダウン

    def __init__(self, pages):
        super().__init__(timeout=120)
        self.page_info = {}

        options = []
        for page in pages[:25]:
            pid = page["id"]
            number = page["properties"]["質問番号"]["number"]
            q = get_question(page)

            self.page_info[pid] = {"number": number, "question": q}
            options.append(discord.SelectOption(label=f"#{number}", value=pid))

        select = discord.ui.Select(
            placeholder="回答する質問番号を選択してください",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        pid = interaction.data["values"][0]
        info = self.page_info[pid]
        number = info["number"]
        question = info["question"]

        await interaction.response.send_modal(
            QAnswerModal(pid, number, question)
        )


class EditSelectView(discord.ui.View):
    # 回答済み質問用の番号選択プルダウン

    def __init__(self, pages):
        super().__init__(timeout=120)
        self.page_info = {}

        options = []
        for page in pages[:25]:
            pid = page["id"]
            number = page["properties"]["質問番号"]["number"]
            q = get_question(page)
            a = get_answer(page)

            self.page_info[pid] = {"number": number, "question": q, "answer": a}
            options.append(discord.SelectOption(label=f"#{number}", value=pid))

        select = discord.ui.Select(
            placeholder="編集する質問番号を選択してください",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        pid = interaction.data["values"][0]
        info = self.page_info[pid]
        number = info["number"]
        question = info["question"]
        answer = info["answer"]

        await interaction.response.send_modal(
            QEditModal(pid, number, question, answer)
        )


# ==============================
# Q&Aコマンド用コマンド
# ==============================
class QACommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    # 回答用コマンドを定義
    @app_commands.command(name="q_answer", description="未回答の質問に回答します")
    async def q_answer(self, interaction: discord.Interaction):
        if not is_qa_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{QA_CHANNEL_ID}> でのみ実行できます。",
                ephemeral=True,
            )

        pages = fetch_unanswered() # 未回答ページ取得
        if not pages:
            return await interaction.response.send_message(
                "未回答の質問はありません。", ephemeral=True
            )

        view = AnswerSelectView(pages)
        await interaction.response.send_message(
            "回答する質問番号を選択してください。",
            view=view,
            ephemeral=True,
        )

    # 回答編集用コマンドを定義
    @app_commands.command(name="q_edit", description="回答済みの質問の回答を編集します")
    async def q_edit(self, interaction: discord.Interaction):
        if not is_qa_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{QA_CHANNEL_ID}> でのみ実行できます。",
                ephemeral=True,
            )

        pages = fetch_answered() # 未回答ページ取得
        if not pages:
            return await interaction.response.send_message(
                "回答済みの質問がありません。", ephemeral=True
            )

        view = EditSelectView(pages)
        await interaction.response.send_message(
            "回答を編集する質問番号を選択してください。",
            view=view,
            ephemeral=True,
        )


# ======================================================
# 自動タスク
# ======================================================
@tasks.loop(hours=24)
async def auto_clean():
    # イベントの過去データ削除（24時間毎）
    delete_past_events()


@tasks.loop(hours=6)
async def auto_check_qa(bot: commands.Bot):
    # Q&A DBの変更監視（6時間毎）
    global FIRST_QA_RUN

    changes = get_qa_changes()

    # 起動直後は通知せず、キャッシュ作成だけ行う
    if FIRST_QA_RUN:
        print("Skipping QA notifications on first run.")

        # FIRST_QA_RUN = False をキャッシュへ保存
        cache = load_cache()
        save_cache(cache, first_run_flag=False)

        FIRST_QA_RUN = False
        return

    # 2回目以降：未回答のものだけ通知
    for ctype, page in changes:
        if get_answer(page) == "(回答なし)":
            await send_qa_notification(bot, ctype, page)


# ======================================================
# Bot 本体
# ======================================================
class MyBot(commands.Bot):
    # コマンド登録
    async def setup_hook(self):
        await self.add_cog(EventCommands(self))
        await self.add_cog(QACommands(self))
        await self.tree.sync()
        print("Slash commands synced")


intents = discord.Intents.default()
bot = MyBot(command_prefix="!", intents=intents)


@bot.event
@bot.event
async def on_ready():
    global FIRST_QA_RUN

    print(f"Bot Ready as {bot.user}")

    # FIRST_QA_RUN をキャッシュから復元
    cache = load_cache()
    FIRST_QA_RUN = cache.get("_first_qa_run", True)

    print("FIRST_QA_RUN =", FIRST_QA_RUN)

    if not auto_clean.is_running():
        auto_clean.start()

    if not auto_check_qa.is_running():
        auto_check_qa.start(bot)

    print("All background tasks started.")


# ===============================
# Bot 起動
# ===============================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
