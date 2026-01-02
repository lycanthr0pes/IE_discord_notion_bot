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


# Discord ScheduledEvent 用：datetime → ISO(JST)
def to_jst_iso(dt: datetime) -> str:
    return dt.astimezone(JST).isoformat()


# ======================================================
# イベント管理機能（Notion 側）
# ======================================================

# イベントをNotionに新規作成（jsonを作成し送信）
def notion_add_event(name, content, date_iso, message_id, creator_id):
    # message_idにDiscordのイベントIDを入れる
    url = "https://api.notion.com/v1/pages"
    data = {
        "parent": {"database_id": NOTION_EVENT_DB_ID},
        "properties": {
            "イベント名": {"title": [{"text": {"content": name}}]},
            "内容": {"rich_text": [{"text": {"content": content}}]},
            "日時": {"date": {"start": date_iso}},
            "メッセージID": {  # = Discord イベントID
                "rich_text": [{"text": {"content": str(message_id)}}]
            },
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
def notion_update_event(
    page_id, name=None, content=None, date_iso=None, message_id=None, page_uuid=None
):
    props = {}
    if name is not None:
        props["イベント名"] = {"title": [{"text": {"content": name}}]}
    if content is not None:
        props["内容"] = {"rich_text": [{"text": {"content": content}}]}
    if date_iso is not None:
        props["日時"] = {"date": {"start": date_iso}}
    if message_id is not None:
        props["メッセージID"] = {
            "rich_text": [{"text": {"content": str(message_id)}}]
        }
    if page_uuid is not None:
        props["ページID"] = {"rich_text": [{"text": {"content": str(page_uuid)}}]}

    res = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=headers,
        json={"properties": props},
    )
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


# 過去(当日より前)のイベントをNotionからアーカイブ扱いで削除する
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

        # 今日より31日前なら削除
        if today.day - dt.day > 30:
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

# イベント名が除外ワード("定例会")を含むかどうか
def is_ignored_event(name: str) -> bool:
    return "定例会" in name


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


def ensure_question_numbers():
    # 質問番号を持たないページにだけ、追加順で新しい番号を付与する
    data = fetch_qa_db()
    if not data:
        return

    pages = data.get("results", [])

    existing_numbers = [
        p["properties"]["質問番号"]["number"]
        for p in pages
        if p["properties"]["質問番号"]["number"] is not None
    ]
    next_num = max(existing_numbers) + 1 if existing_numbers else 1

    # 質問番号がまだ無いページだけ、作成日時昇順で番号割り振り
    missing_pages = [
        p for p in pages if p["properties"]["質問番号"]["number"] is None
    ]
    missing_pages.sort(key=lambda p: p.get("created_time", ""))

    for page in missing_pages:
        page_id = page["id"]
        url = f"https://api.notion.com/v1/pages/{page_id}"
        data = {"properties": {"質問番号": {"number": next_num}}}
        requests.patch(url, headers=headers, json=data)
        next_num += 1

    # ログ出力
    if missing_pages:
        print(f"✅ 新たに {len(missing_pages)} 件の質問番号を採番しました。")


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


async def send_qa_ephemeral(
    interaction: discord.Interaction,
    number,
    question: str,
    answer: str,
    action: str,
):
    # 指定チャンネル内で、回答者本人にのみ見える形で再送
    number_display = number if number is not None else "?"
    msg = (
        f"📩 **{action}（#{number_display}）**\n"
        f"**質問:** {question}\n"
        f"**回答:** {answer}"
    )
    # on_submitでは既にresponseを使っているためfollowupで送信
    await interaction.followup.send(msg, ephemeral=True)


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
        # DMで再送するために質問文を保持しておく
        self.question_text = question_text

        self.answer = discord.ui.TextInput(
            label=f"質問: {question_text}",
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        # Notionに回答を書き込み、成功時のみ回答者へDM再送
        ok = update_answer(self.page_id, self.answer.value)
        if ok:
            await interaction.response.send_message(
                f"✅ 回答を保存しました。（#{self.number}）",
                ephemeral=True,
            )
            # 指定チャンネルで本人にのみ見える形で再送
            await send_qa_ephemeral(
                interaction,
                self.number,
                self.question_text,
                self.answer.value, # 回答を取得
                "回答を保存しました", # アクション内容
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
        # DMで再送するために質問文を保持しておく
        self.question_text = question_text

        self.answer = discord.ui.TextInput(
            label=f"質問: {question_text}",
            style=discord.TextStyle.paragraph,
            default=current_answer,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        # 既存回答を更新し、成功時は回答者へDM再送
        ok = update_answer(self.page_id, self.answer.value)
        if ok:
            await interaction.response.send_message(
                f"✅ 回答を更新しました。（#{self.number}）",
                ephemeral=True,
            )
            # 指定チャンネルで本人にのみ見える形で再送
            await send_qa_ephemeral(
                interaction,
                self.number,
                self.question_text,
                self.answer.value, # 回答内容を取得
                "回答を更新しました", # アクション内容
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
        # Selectの選択時に呼ばれるコールバック関数を紐づける
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        # interaction.data にはSelectの選択結果が入る（max_values=1なので先頭）
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
            options.append(discord.SelectOption(label=f"#{number}", value=pid)) # ラベルはプルダウンの表示名
        # このインタラクションのinteraction.dataにSelectの選択結果が入る
        select = discord.ui.Select(
            placeholder="編集する質問番号を選択してください",
            options=options, # ラベルからvalueを選択し["values"]に格納
            min_values=1, # 一つのみ選択
            max_values=1, # 一つのみ選択
        )
        # Selectの選択時に呼ばれるコールバック関数を紐づける
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        # max_values=1なので先頭
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
        
        # Bot側で自動連番
        ensure_question_numbers()
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
        # Bot側で自動連番
        ensure_question_numbers()
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

    ensure_question_numbers()
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
        await self.add_cog(QACommands(self))
        await self.tree.sync()
        print("Slash commands synced")


intents = discord.Intents.default()
# Discordのイベント機能を使うためのインテント
intents.guild_scheduled_events = True

bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    global FIRST_QA_RUN

    print(f"Bot Ready as {bot.user}")

    # FIRST_QA_RUN をキャッシュから復元
    cache = load_cache()
    FIRST_QA_RUN = cache.get("_first_qa_run", True)

    print("FIRST_QA_RUN =", FIRST_QA_RUN)

    ensure_question_numbers()

    if not auto_clean.is_running():
        auto_clean.start()

    if not auto_check_qa.is_running():
        auto_check_qa.start(bot)

    print("All background tasks started.")


# ======================================================
# Discordイベント機能 → Notion同期部分
# ======================================================

@bot.event
async def on_scheduled_event_create(event):
    """
    Discord のサーバーイベントが作成されたときに呼ばれる
    ここで Notion のイベントDBに登録する
    """
    # 除外ワードを含むイベントは無視
    if is_ignored_event(event.name):
        print(f"⚠️ 除外イベントのため登録しません: {event.name}")
        return
    
    name = event.name
    description = event.description or "(内容なし)"
    start_iso = to_jst_iso(event.start_time)
    creator_id = (
        event.creator_id
        or (event.creator.id if event.creator else "unknown")
    )

    notion_add_event(
        name=name,
        content=description,
        date_iso=start_iso,
        message_id=event.id,  # メッセージID枠にイベントIDを保存
        creator_id=creator_id,
    )

    print(f"🆕 Discordイベント作成 → Notion登録: {name}")


@bot.event
async def on_scheduled_event_update(before, after):
    """
    Discord イベントが更新されたときに呼ばれる
    Notion 側で「メッセージID == after.id」のページを探して更新
    """
    # 除外ワードを含むイベントは無視
    if is_ignored_event(after.name):
        print(f"⚠️ 除外イベントのため更新しません: {after.name}")
        return
    
    pages = fetch_event_pages()
    target = None
    after_id_str = str(after.id)

    for page in pages:
        prop = page["properties"].get("メッセージID", {}).get("rich_text", [])
        if not prop:
            continue
        mid = prop[0]["text"]["content"]
        if mid == after_id_str:
            target = page
            break

    if not target:
        print("⚠️ Notion 側に対応するイベントページが見つかりません。")
        return

    page_id = target["id"]
    new_name = after.name
    new_content = after.description or "(内容なし)"
    new_date_iso = to_jst_iso(after.start_time)

    ok = notion_update_event(
        page_id,
        name=new_name,
        content=new_content,
        date_iso=new_date_iso,
    )
    if ok:
        print(f"✏️ Discordイベント更新 → Notion更新: {new_name}")
    else:
        print("❌ Notion イベント更新に失敗しました。")


@bot.event
async def on_scheduled_event_delete(event):
    """
    Discord イベントが削除されたときに呼ばれる
    Notion 側の対応するページをアーカイブ
    """
    # 除外ワードを含むイベントは無視
    if is_ignored_event(event.name):
        print(f"⚠️ 除外イベントの削除は無視します: {event.name}")
        return
    
    pages = fetch_event_pages()
    target_id = None
    eid = str(event.id)

    for page in pages:
        prop = page["properties"].get("メッセージID", {}).get("rich_text", [])
        if not prop:
            continue
        mid = prop[0]["text"]["content"]
        if mid == eid:
            target_id = page["id"]
            break

    if not target_id:
        print("⚠️ 削除対象の Notion イベントが見つかりません。")
        return

    if notion_delete_event(target_id):
        print(f"🗑️ Discordイベント削除 → Notionイベント削除: {event.name}")
    else:
        print("❌ Notion イベント削除に失敗しました。")


# ===============================
# Bot 起動
# ===============================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
