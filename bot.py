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

# Q&A 用（質問 / 回答）
NOTION_QA_DB_ID = os.getenv("NOTION_QA_ID")

# イベント用（イベント名 / 内容 / 日時）
NOTION_EVENT_DB_ID = os.getenv("NOTION_EVENT_ID")

# 別チャンネル紐付け
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


# ==============================
# チャンネル制限
# ==============================
def is_event_channel(interaction):
    return EVENT_CHANNEL_ID == 0 or interaction.channel_id == EVENT_CHANNEL_ID


def is_qa_channel(interaction):
    return QA_CHANNEL_ID == 0 or interaction.channel_id == QA_CHANNEL_ID


# ==============================
# 日付フォーマット
# ==============================
def format_display_date(date_iso: str):
    dt = datetime.fromisoformat(date_iso)
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    w = weekdays[dt.weekday()]
    try:
        return dt.strftime(f"%#m月%#d日（{w}） %H:%M")
    except:
        return dt.strftime(f"%-m月%-d日（{w}） %H:%M")


def normalize_date(date_str: str, time_str: str):
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y/%m/%d %H:%M")
    dt = dt.replace(tzinfo=JST)
    return dt.isoformat()


# ======================================================
# イベント管理機能
# ======================================================

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
        print("❌ Notion作成エラー:", res.json())
        return None

    page_id = res.json()["id"]
    notion_update_event(page_id, page_uuid=page_id)
    return page_id


def notion_get_event(page_id):
    res = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers)
    return res.json() if "id" in res.json() else None


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


def notion_delete_event(page_id):
    return requests.delete(f"https://api.notion.com/v1/pages/{page_id}", headers=headers).status_code in (200, 201)


# ==============================
# 過去イベント削除（24h）
# ==============================
def delete_past_events():
    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_DB_ID}/query"
    res = requests.post(url, headers=headers, json={}).json()

    today = datetime.now(JST).date()

    for page in res.get("results", []):
        if not page["properties"]["日時"]["date"]:
            continue

        dt = datetime.fromisoformat(page["properties"]["日時"]["date"]["start"]).date()

        if dt < today:
            notion_delete_event(page["id"])
            print(f"[AUTO DELETE] {page['id']} deleted.")


# ==============================
# UI（イベント管理）
# ==============================
class EventEditModal(discord.ui.Modal, title="イベント編集"):
    def __init__(self, page, message_id):
        super().__init__()
        self.page_id = page["id"]
        self.message_id = message_id

        props = page["properties"]

        name_val = props["イベント名"]["title"][0]["text"]["content"]
        content_val = props["内容"]["rich_text"][0]["text"]["content"] if props["内容"]["rich_text"] else ""

        iso = props["日時"]["date"]["start"]
        dt = datetime.fromisoformat(iso)

        self.name = discord.ui.TextInput(label="イベント名", default=name_val)
        self.date = discord.ui.TextInput(label="日付（2025/1/1）", default=f"{dt.year}/{dt.month}/{dt.day}")
        self.time = discord.ui.TextInput(label="時刻（13:00）", default=f"{dt.hour:02d}:{dt.minute:02d}")
        self.content = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph, default=content_val)

        self.add_item(self.name)
        self.add_item(self.date)
        self.add_item(self.time)
        self.add_item(self.content)

    async def on_submit(self, interaction):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ この操作は <#{EVENT_CHANNEL_ID}> のみで可能です。",
                ephemeral=True,
            )

        date_iso = normalize_date(self.date.value, self.time.value)

        notion_update_event(self.page_id, self.name.value, self.content.value, date_iso)

        embed = discord.Embed(title=f"📌 {self.name.value}", color=0x55FF55)
        embed.add_field(name="日時", value=format_display_date(date_iso), inline=False)
        embed.add_field(name="内容", value=self.content.value, inline=False)

        msg = await interaction.channel.fetch_message(int(self.message_id))
        creator_id = notion_get_event(self.page_id)["properties"]["作成者ID"]["rich_text"][0]["text"]["content"]

        await msg.edit(embed=embed, view=EventView(self.page_id, self.message_id, creator_id))
        await interaction.response.send_message("✏️ 更新しました", ephemeral=True)


class EventView(discord.ui.View):
    def __init__(self, page_id, message_id, creator_id):
        super().__init__(timeout=None)
        self.page_id = page_id
        self.message_id = message_id
        self.creator_id = str(creator_id)

    def is_authorized(self, interaction):
        return (
            str(interaction.user.id) == self.creator_id or
            interaction.user.guild_permissions.manage_guild
        )

    @discord.ui.button(label="編集", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction, _):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ この操作は <#{EVENT_CHANNEL_ID}> のみで可能です。",
                ephemeral=True,
            )

        if not self.is_authorized(interaction):
            return await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)

        page = notion_get_event(self.page_id)
        await interaction.response.send_modal(EventEditModal(page, self.message_id))

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction, _):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ この操作は <#{EVENT_CHANNEL_ID}> のみで可能です。",
                ephemeral=True,
            )

        if not self.is_authorized(interaction):
            return await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)

        notion_delete_event(self.page_id)
        await interaction.message.delete()
        await interaction.response.send_message("🗑️ 削除しました", ephemeral=True)


# ==============================
# /event コマンド
# ==============================
class EventCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="event", description="イベントを登録します")
    async def event(self, interaction, name: str, date: str, time: str = "00:00"):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{EVENT_CHANNEL_ID}> でのみ実行できます。",
                ephemeral=True,
            )

        if not re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", date):
            return await interaction.response.send_message("❌ 日付の形式が不正です", ephemeral=True)

        await interaction.response.send_modal(EventCreateModal(name, date, time))


class EventCreateModal(discord.ui.Modal, title="イベント登録"):
    def __init__(self, name, date, time):
        super().__init__()
        self.name = name
        self.date = date
        self.time = time
        self.content = discord.ui.TextInput(label="内容", style=discord.TextStyle.paragraph)
        self.add_item(self.content)

    async def on_submit(self, interaction):
        if not is_event_channel(interaction):
            return await interaction.response.send_message(
                f"❌ <#{EVENT_CHANNEL_ID}> でのみ使用できます。",
                ephemeral=True,
            )

        content = self.content.value or "(内容なし)"
        date_iso = normalize_date(self.date, self.time)

        embed = discord.Embed(title=f"📌 {self.name}", color=0x00AAFF)
        embed.add_field(name="日時", value=format_display_date(date_iso), inline=False)
        embed.add_field(name="内容", value=content, inline=False)

        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()

        page_id = notion_add_event(self.name, content, date_iso, msg.id, interaction.user.id)
        await msg.edit(embed=embed, view=EventView(page_id, msg.id, interaction.user.id))


# ======================================================
# ここから Q&A 機能
# ======================================================

def fetch_qa_db():
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    res = requests.post(url, headers=headers)
    return res.json() if res.status_code == 200 else None


CACHE_FILE = "notion_cache.json"


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def get_changes():
    data = fetch_qa_db()
    if not data:
        return []
    cache = load_cache()
    new_cache = {}
    changes = []
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


def get_question(page):
    t = page["properties"]["質問"]["title"]
    return t[0]["plain_text"] if t else "(質問なし)"


def get_answer(page):
    t = page["properties"]["回答"]["rich_text"]
    return t[0]["plain_text"] if t else "(回答なし)"


def fetch_unanswered():
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    data = {"filter": {"property": "回答", "rich_text": {"is_empty": True}}}
    res = requests.post(url, headers=headers, json=data)
    return res.json().get("results", []) if res.status_code == 200 else []


def update_answer(page_id, answer):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {
        "properties": {
            "回答": {
                "rich_text": [{"type": "text", "text": {"content": answer}}]
            }
        }
    }
    return requests.patch(url, headers=headers, json=data).status_code == 200


async def send_qa_notification(bot, ctype, page):
    if QA_CHANNEL_ID == 0:
        return
    ch = await bot.fetch_channel(QA_CHANNEL_ID)
    q = get_question(page)
    a = get_answer(page)
    if ctype == "new":
        msg = f"🆕 **新しい質問が追加されました！**\n**質問:** {q}\n**回答:** {a}"
    else:
        msg = f"✏️ **回答が更新されました！**\n**質問:** {q}\n**回答:** {a}"
    await ch.send(msg)


class AnswerModal(discord.ui.Modal, title="回答入力"):
    def __init__(self, page_id, question):
        super().__init__()
        self.page_id = page_id
        self.ans = discord.ui.TextInput(label="回答", style=discord.TextStyle.paragraph)
        self.add_item(self.ans)

    async def on_submit(self, interaction):
        if update_answer(self.page_id, self.ans.value):
            await interaction.response.send_message("✅ 保存しました。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 更新に失敗しました。", ephemeral=True)


class AnswerSelectView(discord.ui.View):
    def __init__(self, pages):
        super().__init__(timeout=120)
        self.questions = {p["id"]: get_question(p) for p in pages[:25]}
        options = [
            discord.SelectOption(label=(q if len(q) <= 100 else q[:97]+"..."), value=pid)
            for pid, q in self.questions.items()
        ]
        select = discord.ui.Select(placeholder="質問を選択", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction):
        pid = interaction.data["values"][0]
        await interaction.response.send_modal(AnswerModal(pid, self.questions[pid]))


class QACommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="answer", description="未回答の質問に回答します")
    async def answer(self, interaction):
        if not is_qa_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{QA_CHANNEL_ID}> でのみ実行できます。",
                ephemeral=True,
            )

        pages = fetch_unanswered()
        if not pages:
            return await interaction.response.send_message(
                "未回答の質問はありません。", ephemeral=True
            )

        await interaction.response.send_message(
            "回答する質問を選択してください", view=AnswerSelectView(pages), ephemeral=True
        )


# ======================================================
# 自動タスク
# ======================================================
@tasks.loop(hours=24)
async def auto_clean():
    delete_past_events()


@tasks.loop(hours=6)
async def auto_check_qa(bot):
    changes = get_changes()
    for ctype, page in changes:
        await send_qa_notification(bot, ctype, page)


# ======================================================
# Bot 本体
# ======================================================
class MyBot(commands.Bot):
    async def setup_hook(self):
        await self.add_cog(EventCommands(self))
        await self.add_cog(QACommands(self))
        await self.tree.sync()
        print("Commands synced")


intents = discord.Intents.default()
bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Bot Ready as {bot.user}")

    # イベント自動削除タスク
    if not auto_clean.is_running():
        auto_clean.start()

    # Q&A の自動チェックタスク
    if not auto_check_qa.is_running():
        auto_check_qa.start(bot)

    print("All background tasks started.")


# ===============================
# Bot 起動
# ===============================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)