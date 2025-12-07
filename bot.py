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
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", 0))

NOTION_TOKEN = os.getenv("NOTION_TOKEN")

# Q&A 用 Notion DB（質問 / 回答）  ※元 Bot.py
NOTION_QA_DB_ID = os.getenv("NOTION_DATABASE_ID")

# イベント用 Notion DB（イベント名 / 内容 / 日時 など） ※元 bot.py
NOTION_EVENT_DB_ID = os.getenv("NOTION_DB_ID")

# ==============================
# 定数 / 共通ヘッダ
# ==============================
JST = timezone(timedelta(hours=9))

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# ==============================
# チャンネル制限チェック（イベント & Q&A 共用）
# ==============================
def check_channel(interaction: discord.Interaction):
    return DISCORD_CHANNEL_ID == 0 or interaction.channel_id == DISCORD_CHANNEL_ID


# ==============================
# 日付フォーマット（曜日入り）
# ==============================
def format_display_date(date_iso: str):
    dt = datetime.fromisoformat(date_iso)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    w = weekdays[dt.weekday()]
    try:
        # Windows向け
        return dt.strftime(f"%#m月%#d日（{w}） %H:%M")
    except Exception:
        # Linuxなど
        return dt.strftime(f"%-m月%-d日（{w}） %H:%M")


# ==============================
# ISO8601 作成
# ==============================
def normalize_date(date_str: str, time_str: str):
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
    dt = dt.replace(tzinfo=JST)
    return dt.isoformat()


# ======================================================
# ここから「イベント管理」機能（元 bot.py）
# ======================================================

# ==============================
# Notion 新規作成（イベント）
# ==============================
def notion_add(event_name, content, date_iso, message_id=None, creator_id=None):
    url = "https://api.notion.com/v1/pages"
    data = {
        "parent": {"database_id": NOTION_EVENT_DB_ID},
        "properties": {
            "イベント名": {"title": [{"text": {"content": event_name}}]},
            "内容": {"rich_text": [{"text": {"content": content}}]},
            "日時": {"date": {"start": date_iso}},
            "メッセージID": {"rich_text": [{"text": {"content": str(message_id)}}]},
            "作成者ID": {"rich_text": [{"text": {"content": str(creator_id)}}]},
            "ページID": {"rich_text": [{"text": {"content": ""}}]},
        },
    }

    res = requests.post(url, headers=headers, json=data)
    if res.status_code not in (200, 201):
        print("❌ Notion ページ作成失敗:", res.json())
        return None

    page_id = res.json()["id"]
    notion_update(page_id, page_uuid=page_id)
    return page_id


# ==============================
# Notion ページ取得（イベント）
# ==============================
def notion_get_page(page_id):
    res = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    data = res.json()

    if "id" not in data:
        print("❌ Notion ページ取得失敗:", data)
        return None

    return data


# ==============================
# Notion 更新（イベント）
# ==============================
def notion_update(page_id, name=None, content=None, date_iso=None, message_id=None, page_uuid=None):
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

    url = f"https://api.notion.com/v1/pages/{page_id}"
    res = requests.patch(url, headers=headers, json={"properties": props})
    return res.status_code in (200, 201)


# ==============================
# Notion 検索（イベント）
# ==============================
def notion_search(event_name):
    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_DB_ID}/query"
    query = {
        "filter": {
            "property": "イベント名",
            "title": {"contains": event_name},
        }
    }
    res = requests.post(url, headers=headers, json=query).json()
    results = res.get("results", [])
    return results[0] if results else None


# ==============================
# Notion 削除（イベント）
# ==============================
def notion_delete(page_id):
    res = requests.delete(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    return res.status_code in (200, 201)


# ==============================
# 過去イベント自動削除
# ==============================
def delete_past_events():
    if NOTION_EVENT_DB_ID is None:
        return

    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_DB_ID}/query"
    res = requests.post(url, headers=headers, json={}).json()

    today = datetime.now(JST).date()

    for page in res.get("results", []):
        date_prop = page["properties"]["日時"]["date"]
        if not date_prop:
            continue

        date_iso = date_prop["start"]
        event_date = datetime.fromisoformat(date_iso).date()

        if event_date < today:
            notion_delete(page["id"])
            print(f"[AUTO DELETE] {page['id']} を削除 ({event_date})")


# ======================================================
# GUI ボタン（編集／削除）本人＋管理者限定
# ======================================================
class EventView(discord.ui.View):
    def __init__(self, page_id, message_id, creator_id):
        super().__init__(timeout=None)
        self.page_id = page_id
        self.message_id = message_id
        self.creator_id = str(creator_id)

    def has_permission(self, interaction: discord.Interaction) -> bool:
        return (
            str(interaction.user.id) == self.creator_id
            or interaction.user.guild_permissions.manage_guild
        )

    @discord.ui.button(label="編集", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.has_permission(interaction):
            return await interaction.response.send_message(
                "❌ 編集できるのは作成者本人または管理者のみです。",
                ephemeral=True,
            )

        page = notion_get_page(self.page_id)
        if page is None:
            return await interaction.response.send_message("❌ Notion ページが見つかりません。", ephemeral=True)

        await interaction.response.send_modal(EventEditModal(page, self.message_id))

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.has_permission(interaction):
            return await interaction.response.send_message(
                "❌ 削除できるのは作成者本人または管理者のみです。",
                ephemeral=True,
            )

        notion_delete(self.page_id)
        await interaction.message.delete()
        await interaction.response.send_message("🗑️ 削除しました", ephemeral=True)


# ======================================================
# 新規登録 Modal（イベント）
# ======================================================
class EventCreateModal(discord.ui.Modal, title="イベント内容の入力"):
    def __init__(self, name, date, time):
        super().__init__()
        self.event_name = name
        self.date = date
        self.time = time

        self.content = discord.ui.TextInput(
            label="内容（改行自由）",
            style=discord.TextStyle.paragraph,
            required=False,
            placeholder="例：\n準備物の確認\n次回予定の相談",
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction):
        content = self.content.value or "(内容なし)"
        date_iso = normalize_date(self.date, self.time)

        embed = discord.Embed(
            title=f"📌 {self.event_name}",
            color=0x00AAFF,
        )
        embed.add_field(name="日時", value=format_display_date(date_iso), inline=False)
        embed.add_field(name="内容", value=content, inline=False)

        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()

        page_id = notion_add(self.event_name, content, date_iso, msg.id, interaction.user.id)

        await msg.edit(embed=embed, view=EventView(page_id, msg.id, interaction.user.id))


# ======================================================
# 編集 Modal（イベント）
# ======================================================
class EventEditModal(discord.ui.Modal, title="イベントの編集"):
    def __init__(self, page, message_id):
        super().__init__()
        self.page_id = page["id"]
        self.message_id = message_id

        props = page["properties"]

        name_val = props["イベント名"]["title"][0]["text"]["content"]
        if props["内容"]["rich_text"]:
            content_val = props["内容"]["rich_text"][0]["text"]["content"]
        else:
            content_val = ""

        iso = props["日時"]["date"]["start"]
        dt = datetime.fromisoformat(iso)

        date_str = f"{dt.year}/{dt.month}/{dt.day}"
        time_str = f"{dt.hour:02d}:{dt.minute:02d}"

        self.name = discord.ui.TextInput(label="イベント名", default=name_val)
        self.date = discord.ui.TextInput(label="日付（例：2025/1/1）", default=date_str)
        self.time = discord.ui.TextInput(label="時刻（例：13:00）", default=time_str)
        self.content = discord.ui.TextInput(
            label="内容（改行自由）",
            style=discord.TextStyle.paragraph,
            default=content_val,
        )

        self.add_item(self.name)
        self.add_item(self.date)
        self.add_item(self.time)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name.value
        date = self.date.value
        time = self.time.value
        content = self.content.value

        if not re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", date):
            return await interaction.response.send_message("❌ 日付形式が不正です", ephemeral=True)

        if not re.match(r"^\d{1,2}:\d{2}$", time):
            time = "00:00"

        date_iso = normalize_date(date, time)

        notion_update(self.page_id, name, content, date_iso)

        updated = discord.Embed(
            title=f"📌 {name}",
            color=0x55FF55,
        )
        updated.add_field(name="日時", value=format_display_date(date_iso), inline=False)
        updated.add_field(name="内容", value=content, inline=False)

        msg = await interaction.channel.fetch_message(int(self.message_id))

        page = notion_get_page(self.page_id)
        creator_id = page["properties"]["作成者ID"]["rich_text"][0]["text"]["content"]

        await msg.edit(embed=updated, view=EventView(self.page_id, self.message_id, creator_id))

        await interaction.response.send_message("✏️ 更新しました", ephemeral=True)


# ======================================================
# Slash Commands（イベント）
# ======================================================
class EventCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="event", description="イベントを登録します")
    @app_commands.describe(
        name="イベント名",
        date="日付（例：2025/1/1）",
        time="時刻（例：13:00）",
    )
    async def event(self, interaction: discord.Interaction, name: str, date: str, time: str = "00:00"):
        if not check_channel(interaction):
            return await interaction.response.send_message(
                f"❌ <#{DISCORD_CHANNEL_ID}> のみ実行可能",
                ephemeral=True,
            )

        if not re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", date):
            return await interaction.response.send_message(
                "❌ 日付形式は例：2025/1/1",
                ephemeral=True,
            )

        if not re.match(r"^\d{1,2}:\d{2}$", time):
            time = "00:00"

        await interaction.response.send_modal(EventCreateModal(name, date, time))

    @app_commands.command(name="event_edit", description="イベントを編集します")
    @app_commands.describe(target="編集するイベント名（部分一致可）")
    async def event_edit(self, interaction: discord.Interaction, target: str):
        if not check_channel(interaction):
            return await interaction.response.send_message(
                f"❌ <#{DISCORD_CHANNEL_ID}> のみ実行可能",
                ephemeral=True,
            )

        page = notion_search(target)
        if not page:
            return await interaction.response.send_message("❌ 該当イベントがありません", ephemeral=True)

        message_id = page["properties"]["メッセージID"]["rich_text"][0]["text"]["content"]

        await interaction.response.send_modal(EventEditModal(page, message_id))


# ======================================================
# ここから「Q&A」機能（元 Bot.py を commands.Bot 用に移植）
# ======================================================

# ==============================
# Q&A用 Notion DB 取得
# ==============================
def fetch_notion_qa_database():
    if NOTION_QA_DB_ID is None:
        return None
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    res = requests.post(url, headers=headers)
    if res.status_code != 200:
        print(f"Notion QA API Error: {res.text}")
        return None
    return res.json()


# ==============================
# 差分チェック（キャッシュ比較）
# ==============================
CACHE_FILE = "notion_cache.json"


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get_changes():
    data = fetch_notion_qa_database()
    if not data:
        return []

    cache = load_cache()
    new_cache = {}
    changes = []

    for page in data.get("results", []):
        page_id = page["id"]
        last_edit = page["last_edited_time"]
        new_cache[page_id] = last_edit

        if page_id not in cache:
            changes.append(("new", page))
        elif cache[page_id] != last_edit:
            changes.append(("update", page))

    save_cache(new_cache)
    return changes


# ==============================
# Notion ページから質問と回答を取得
# ==============================
def get_question(page):
    q = page["properties"]["質問"]["title"]
    return q[0]["plain_text"] if q else "(質問なし)"


def get_answer(page):
    a = page["properties"]["回答"]["rich_text"]
    return a[0]["plain_text"] if a else "(回答なし)"


# ==============================
# 未回答の質問一覧取得
# ==============================
def fetch_unanswered_questions():
    if NOTION_QA_DB_ID is None:
        return []

    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    data = {
        "filter": {
            "property": "回答",
            "rich_text": {
                "is_empty": True
            }
        }
    }

    res = requests.post(url, headers=headers, json=data)
    if res.status_code != 200:
        print("Notion QA unanswered error:", res.text)
        return []

    return res.json().get("results", [])


# ==============================
# Notion更新 → Discord通知
# ==============================
async def send_update_message(bot: commands.Bot, change_type, page):
    if DISCORD_CHANNEL_ID == 0:
        return

    channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)

    question = get_question(page)
    answer = get_answer(page)

    if change_type == "new":
        msg = (
            f"🆕 **新しい質問が追加されました！**\n"
            f"**質問:** {question}\n"
            f"**回答:** {answer}"
        )
    else:
        msg = (
            f"✏️ **質問の回答が更新されました！**\n"
            f"**質問:** {question}\n"
            f"**回答:** {answer}"
        )

    await channel.send(msg)


# ==============================
# Notion 回答を更新
# ==============================
def update_notion_answer(page_id, new_answer):
    url = f"https://api.notion.com/v1/pages/{page_id}"

    data = {
        "properties": {
            "回答": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": new_answer},
                    }
                ]
            }
        }
    }

    res = requests.patch(url, headers=headers, json=data)

    if res.status_code != 200:
        print("Update error:", res.text)
        return False

    return True


# ======================================================
# /answer 用 UI （Select + Modal）
# ======================================================
class AnswerModal(discord.ui.Modal, title="回答の入力"):
    def __init__(self, page_id: str, question_text: str):
        super().__init__(title="回答の入力")
        self.page_id = page_id
        self.question_text = question_text

        self.answer = discord.ui.TextInput(
            label="回答",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        ok = update_notion_answer(self.page_id, self.answer.value)

        if ok:
            await interaction.response.send_message(
                "✅ 回答を保存しました。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ 回答の保存に失敗しました。", ephemeral=True
            )


class AnswerSelectView(discord.ui.View):
    def __init__(self, pages):
        super().__init__(timeout=180)
        self.page_map = {}

        options = []
        for page in pages[:25]:  # DiscordのSelectは最大25件
            page_id = page["id"]
            question_text = get_question(page)
            label = question_text if len(question_text) <= 100 else question_text[:97] + "..."
            options.append(discord.SelectOption(label=label, value=page_id))
            self.page_map[page_id] = question_text

        select = discord.ui.Select(
            placeholder="回答する質問を選択してください",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        page_id = interaction.data["values"][0]
        question_text = self.page_map.get(page_id, "")
        await interaction.response.send_modal(AnswerModal(page_id, question_text))


# ======================================================
# Slash Commands（Q&A 用）
# ======================================================
class QACommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="answer", description="未回答の質問リストから選択して回答します")
    async def answer(self, interaction: discord.Interaction):
        if not check_channel(interaction):
            return await interaction.response.send_message(
                f"❌ <#{DISCORD_CHANNEL_ID}> のみ実行可能",
                ephemeral=True,
            )

        pages = fetch_unanswered_questions()
        if not pages:
            return await interaction.response.send_message(
                "未回答の質問はありません。", ephemeral=True
            )

        view = AnswerSelectView(pages)
        await interaction.response.send_message(
            "未回答の質問を選択してください。", view=view, ephemeral=True
        )


# ======================================================
# 自動タスク
# ======================================================
@tasks.loop(hours=6)
async def auto_clean():
    delete_past_events()


@tasks.loop(hours=6)
async def check_notion(bot: commands.Bot):
    print("Checking Notion QA DB...")
    changes = get_changes()
    for ctype, page in changes:
        await send_update_message(bot, ctype, page)


# ======================================================
# Bot 本体
# ======================================================
class MyBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(EventCommands(self))
        await self.add_cog(QACommands(self))
        await self.tree.sync()
        print("Slash Commands Synced")


intents = discord.Intents.default()
bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Bot Ready as {bot.user}")

    if not auto_clean.is_running():
        auto_clean.start()

    if not check_notion.is_running():
        check_notion.start(bot)


# ======================================================
# Bot 起動
# ======================================================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)