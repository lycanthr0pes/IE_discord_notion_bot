import os
import json
import logging
import aiohttp
from google.oauth2 import service_account
from googleapiclient.discovery import build
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone, timedelta

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bot")

# 空文字や前後空白込みの環境変数を安全に扱う
def getenv_clean(name: str, default=None):
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
        return value if value else default
    return value

# ==============================
# 環境変数
# ==============================
DISCORD_TOKEN = getenv_clean("DISCORD_TOKEN")
NOTION_TOKEN = getenv_clean("NOTION_TOKEN")

# Q&A 用（質問 / 回答 / 質問番号）
NOTION_QA_DB_ID = getenv_clean("NOTION_QA_ID")

# イベント用（外部用: イベント名 / 内容 / 日時 / メッセージID / 作成者ID / ページID / GoogleイベントID）
NOTION_EVENT_EXTERNAL_DB_ID = getenv_clean("NOTION_EVENT_ID")

# イベント用（内部用: イベント名 / 内容 / 日時 / 場所 / メッセージID / 作成者ID / ページID / イベントURL / GoogleイベントID
NOTION_EVENT_INTERNAL_DB_ID = getenv_clean("NOTION_EVENT_INTERNAL_ID")

# Googleカレンダー連携
GOOGLE_CALENDAR_ID = getenv_clean("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")

# チャンネル紐付け
QA_CHANNEL_ID = int(os.getenv("QA_CHANNEL_ID", 0))
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID", 0))
REMINDER_ROLE_ID = int(os.getenv("REMINDER_ROLE_ID", 0))
REMINDER_WINDOW_MINUTES = int(os.getenv("REMINDER_WINDOW_MINUTES", 15))

# ==============================
# 共通 Notion ヘッダ
# ==============================
headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def notion_request(method: str, url: str, json_body=None):
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.request(method, url, headers=headers, json=json_body) as res:
            text = await res.text()
            data = None
            if text:
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = None
            return res.status, text, data

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


GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_google_service = None

def load_service_account_info():
    # ------------------------------------------------------------
    # Google Service Account の認証情報(JSON/dict)を読み込んで返す。
    #
    # 入力:
    # - 環境変数を参照
    # - GOOGLE_SERVICE_ACCOUNT_JSON
    #   1) JSON文字列そのもの
    #   2) JSONファイルのパス
    # - GOOGLE_SERVICE_ACCOUNT_JSON_PATH
    #   明示的なJSONファイルパス
    #
    # 出力:
    # - 成功: service_account_info(dict)
    # - 失敗: None
    # ------------------------------------------------------------
    json_env = GOOGLE_SERVICE_ACCOUNT_JSON
    if json_env:
        # ケース1: 環境変数がファイルパス
        if os.path.exists(json_env):
            try:
                with open(json_env, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("GoogleサービスアカウントJSON読み込み失敗(path): %s", exc)
                return None
        try:
            # ケース2: 環境変数がJSON文字列
            return json.loads(json_env)
        except json.JSONDecodeError:
            # 文字列はあるがJSONとして壊れている
            logger.error(
                "GOOGLE_SERVICE_ACCOUNT_JSON は有効なJSON文字列でもファイルパスでもありません。"
            )
            return None

    # 明示パス指定を利用
    if GOOGLE_SERVICE_ACCOUNT_JSON_PATH:
        if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON_PATH):
            logger.error(
                "GOOGLE_SERVICE_ACCOUNT_JSON_PATH のファイルが存在しません: %s",
                GOOGLE_SERVICE_ACCOUNT_JSON_PATH,
            )
            return None
        try:
            with open(GOOGLE_SERVICE_ACCOUNT_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error("GoogleサービスアカウントJSON読み込み失敗(path): %s", exc)
            return None
    # 認証情報を解決できなかった
    logger.warning(
        "Google連携無効: GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SERVICE_ACCOUNT_JSON_PATH が未設定です。"
    )
    return None

def get_google_calendar_service():
    # ------------------------------------------------------------
    # Google Calendar API クライアントを生成し、グローバルにキャッシュして返す。
    #
    # 入力:
    # - 直接引数はなし
    # - GOOGLE_CALENDAR_ID
    # - load_service_account_info() の返り値
    #
    # 出力:
    # - 成功: googleapiclient.discovery.build(...) の service オブジェクト
    # - 失敗: None
    #
    # 失敗条件:
    # - カレンダーID未設定
    # - サービスアカウント情報の読み込み失敗
    #
    # 備考:
    # 一度生成した service は _google_service に保持し、再利用してAPI初期化コストを抑える。
    # ------------------------------------------------------------
    global _google_service
    # 既に初期化済みならそのまま返す
    if _google_service is not None:
        return _google_service
    # カレンダーIDが無い場合は連携不能
    if not GOOGLE_CALENDAR_ID:
        logger.warning("Google連携無効: GOOGLE_CALENDAR_ID が未設定です。")
        return None
    # 認証情報をロード
    info = load_service_account_info()
    # 認証情報が取得できなければ連携不能
    if not info:
        return None
    # Service Account から認証情報を生成して Calendar API client を作成
    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)
        _google_service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _google_service
    except Exception as exc:
        logger.error("Google Calendar service 初期化失敗: %s", exc)
        return None


def validate_google_calendar_connection():
    # 起動時に設定/権限の妥当性を確認し、失敗理由をログへ出す。
    service = get_google_calendar_service()
    if not service:
        return False
    try:
        service.calendars().get(calendarId=GOOGLE_CALENDAR_ID).execute()
        logger.info("Googleカレンダー接続確認OK: %s", GOOGLE_CALENDAR_ID)
        return True
    except Exception as exc:
        logger.error(
            "Googleカレンダー接続確認失敗。カレンダー共有設定/ID/権限を確認してください: %s",
            exc,
        )
        return False

def google_add_event(name, description, start_dt, end_dt, location=None):
    # ------------------------------------------------------------
    # Discordのイベント情報をGoogle Calendarへ登録する。
    #
    # 引数:
    # - name: 予定タイトル
    # - description: 予定説明
    # - start_dt: 開始日時(datetime)
    # - end_dt: 終了日時(datetime)
    # - location: 場所情報（任意）
    #
    # 出力:
    # - 成功: Google Calendar API の insert レスポンス(dict)
    # - 失敗: None
    #
    # 処理概要:
    # 1) Calendar API service を取得
    # 2) 日時をJST ISO文字列へ変換
    # 3) events.insert を実行
    #
    # 備考:
    # エラー時は例外を握りつぶして None を返す。
    # ------------------------------------------------------------
    service = get_google_calendar_service()
    # Google service が無ければスキップ
    if not service:
        logger.warning("Googleカレンダー登録をスキップ: 連携設定が有効化されていません。")
        return None
    start_iso = to_jst_iso(start_dt)
    end_iso = to_jst_iso(end_dt)
    body = {
        "summary": name,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_iso, "timeZone": "Asia/Tokyo"},
    }
    if location:
        body["location"] = str(location)
    try:
        # Calendar に予定を作成し、APIレスポンスを返す
        return (
            service.events()
            .insert(calendarId=GOOGLE_CALENDAR_ID, body=body)
            .execute()
        )
    except Exception as exc:
        logger.error("Googleカレンダー追加失敗: %s", exc)
        return None


def google_update_event(google_event_id, name, description, start_dt, end_dt, location=None):
    # Google Calendar の既存イベントを更新する。
    service = get_google_calendar_service()
    if not service or not google_event_id:
        return None
    start_iso = to_jst_iso(start_dt)
    end_iso = to_jst_iso(end_dt)
    body = {
        "summary": name,
        "description": description,
        "start": {"dateTime": start_iso, "timeZone": "Asia/Tokyo"},
        "end": {"dateTime": end_iso, "timeZone": "Asia/Tokyo"},
    }
    if location:
        body["location"] = str(location)
    try:
        return (
            service.events()
            .patch(calendarId=GOOGLE_CALENDAR_ID, eventId=google_event_id, body=body)
            .execute()
        )
    except Exception as exc:
        logger.error("Googleカレンダー更新失敗(event_id=%s): %s", google_event_id, exc)
        return None


def google_delete_event(google_event_id):
    # ------------------------------------------------------------
    # Google Calendar の既存イベントを削除する。
    #
    # 引数:
    # - google_event_id: 削除対象のGoogleイベントID
    #
    # 出力:
    # - 成功: True
    # - 失敗: False
    #
    # 備考:
    # service未初期化 / ID未指定時は削除を行わず False を返す。
    # ------------------------------------------------------------
    service = get_google_calendar_service()
    if not service or not google_event_id:
        return False
    try:
        service.events().delete(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=google_event_id,
        ).execute()
        return True
    except Exception as exc:
        logger.error("Googleカレンダー削除失敗(event_id=%s): %s", google_event_id, exc)
        return False


# ======================================================
# イベント管理機能（Notion 側）
# ======================================================

# イベントをNotionに新規作成（jsonを作成し送信）
async def notion_add_event(
    db_id,
    name,
    content,
    date_iso,
    message_id,
    creator_id,
    event_url=None,
    google_event_id=None,
    location=None,
):
    # ------------------------------------------------------------
    # Notion DB にイベントページを新規作成する。
    #
    # 引数:
    # - db_id: 登録先Notion DB ID
    # - name: イベント名
    # - content: イベント内容
    # - date_iso: ISO形式の開始日時
    # - message_id: DiscordイベントID（メッセージID列に保存）
    # - creator_id: 作成者ID
    # - event_url: 任意。イベントURL（内部DB向け）
    # - google_event_id: 任意。GoogleイベントID（相互参照用）
    #
    # 出力:
    # - 成功: 作成した Notion ページID(str)
    # - 失敗: None
    #
    # 備考:
    # 作成直後に notion_update_event(..., page_uuid=page_id) を呼び、
    # 「ページID」プロパティへページ自身のIDを反映する。
    # ------------------------------------------------------------
    # message_idにDiscordのイベントIDを入れる
    if not db_id:
        return None
    url = "https://api.notion.com/v1/pages"
    data = {
        "parent": {"database_id": db_id},
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
    # 内部用DBではイベントURLも保存する（NotionのURLプロパティ）
    if event_url is not None:
        data["properties"]["イベントURL"] = {"url": event_url}
    if google_event_id is not None:
        # GoogleイベントIDで相互参照できるように保存
        data["properties"]["GoogleイベントID"] = {
            "rich_text": [{"text": {"content": str(google_event_id)}}]
        }
    if location is not None:
        data["properties"]["場所"] = {
            "rich_text": [{"text": {"content": str(location)}}]
        }
    status, text, res_data = await notion_request("POST", url, json_body=data)
    if status not in (200, 201):
        # ログ出力
        logger.error("Notion作成エラー: %s", text)
        return None

    # ページIDを追加
    page_id = res_data["id"]
    await notion_update_event(page_id, page_uuid=page_id)
    return page_id


# Notion APIを使ってNotion上のイベントを取得し、JSONデータを返す
async def notion_get_event(page_id):
    # ------------------------------------------------------------
    # page_id をキーに Notion のイベントページを1件取得する。
    #
    # 引数:
    # - page_id: NotionページID
    #
    # 出力:
    # - 成功: ページJSON(dict)
    # - 失敗: None
    # ------------------------------------------------------------
    status, _text, data = await notion_request(
        "GET",
        f"https://api.notion.com/v1/pages/{page_id}",
    )
    if status != 200 or not data:
        return None
    return data if "id" in data else None


# 指定されたNotionイベントのプロパティを更新する
async def notion_update_event(
    page_id,
    name=None,
    content=None,
    date_iso=None,
    message_id=None,
    page_uuid=None,
    event_url=None,
    google_event_id=None,
    location=None,
):
    # ------------------------------------------------------------
    # 既存のNotionイベントページを部分更新する。
    #
    # 引数:
    # - page_id: 更新対象ページID
    # - name/content/date_iso/message_id/page_uuid/event_url/google_event_id:
    #   None 以外の項目だけ更新対象に含める
    #
    # 出力:
    # - 成功: True
    # - 失敗: False
    # ------------------------------------------------------------
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
    if event_url is not None:
        props["イベントURL"] = {"url": event_url}
    if google_event_id is not None:
        # GoogleイベントIDで相互参照できるように保存
        props["GoogleイベントID"] = {
            "rich_text": [{"text": {"content": str(google_event_id)}}]
        }
    if location is not None:
        props["場所"] = {"rich_text": [{"text": {"content": str(location)}}]}

    status, _text, _data = await notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        json_body={"properties": props},
    )
    return status in (200, 201)


# イベントをNotionからアーカイブ扱いで削除する
async def notion_delete_event(page_id):
    # ------------------------------------------------------------
    # Notionページをアーカイブ扱いで削除する（archived=True）。
    #
    # 引数:
    # - page_id: 対象ページID
    #
    # 出力:
    # - 成功: True
    # - 失敗: False
    # ------------------------------------------------------------
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {"archived": True}

    status, text, _res_data = await notion_request("PATCH", url, json_body=data)

    #ログ出力
    if status not in (200, 201):
        logger.error("Notion削除エラー: %s", text)
        return False

    return True


# 過去(当日より前)のイベントをNotionからアーカイブ扱いで削除する
async def delete_past_events_for_db(db_id):
    # ------------------------------------------------------------
    # 指定DB内のイベントを走査し、30日以上前の予定をアーカイブする。
    #
    # 引数:
    # - db_id: 対象Notion DB ID
    #
    # 出力:
    # - なし
    #
    # 判定ルール:
    # - 日時.start を日付化
    # - (today - event_date).days >= 30 を削除対象とする
    # ------------------------------------------------------------
    if not db_id:
        return
    # クエリを送って全イベントを取得（json）
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    status, _text, data = await notion_request("POST", url, json_body={})
    if status != 200 or not data:
        return

    # 日付(日本時間)を取得
    today = datetime.now(JST).date()

    # jsonから各イベントの日時プロパティを確認
    for page in data.get("results", []):
        date_prop = page["properties"]["日時"]["date"]
        if not date_prop:
            continue

        # 日付(ISO形式)をdatetimeに変換する(startは日時プロパティの開始日時)
        dt = datetime.fromisoformat(date_prop["start"]).date()

        # 今日から30日以上前なら削除
        if (today - dt).days >= 30:
            await notion_request(
                "PATCH",
                f"https://api.notion.com/v1/pages/{page['id']}",
                json_body={"archived": True},
            )
            # ログ出力
            logger.info("[AUTO DELETE] %s をアーカイブ（削除）しました (%s)", page["id"], dt)


async def delete_finished_events_for_db(db_id):
    # ------------------------------------------------------------
    # 指定DB内のイベントを走査し、終了時刻を過ぎた予定をアーカイブする。
    #
    # 引数:
    # - db_id: 対象Notion DB ID
    #
    # 出力:
    # - なし
    #
    # 判定ルール:
    # - end があれば end、無ければ start を終了時刻として扱う
    # - end_dt <= now なら削除対象
    # ------------------------------------------------------------
    if not db_id:
        return
    # クエリを送って全イベントを取得（json）
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    status, _text, data = await notion_request("POST", url, json_body={})
    if status != 200 or not data:
        return

    now = datetime.now(JST)

    for page in data.get("results", []):
        date_prop = page["properties"]["日時"]["date"]
        if not date_prop:
            continue

        # endがあればend、それ以外はstartを終了時刻として扱う
        end_iso = date_prop.get("end") or date_prop.get("start")
        if not end_iso:
            continue

        end_dt = datetime.fromisoformat(end_iso)

        if end_dt <= now:
            await notion_request(
                "PATCH",
                f"https://api.notion.com/v1/pages/{page['id']}",
                json_body={"archived": True},
            )
            logger.info("[AUTO DELETE] %s を終了時刻によりアーカイブしました (%s)", page["id"], end_dt)


async def delete_past_events():
    # ------------------------------------------------------------
    # DB種別ごとの自動削除ポリシーをまとめて実行する。
    #
    # 実行内容:
    # - 外部用DB: 30日以上前のイベントを削除
    # - 内部用DB: 終了時刻を過ぎたイベントを削除
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # 外部用は30日以上前で削除
    await delete_past_events_for_db(NOTION_EVENT_EXTERNAL_DB_ID)
    # 内部用は終了時刻を過ぎたら削除
    await delete_finished_events_for_db(NOTION_EVENT_INTERNAL_DB_ID)


# クエリを送って全イベントを取得（json）
async def fetch_event_pages(db_id):
    # ------------------------------------------------------------
    # 指定Notion DBのページ一覧を取得する。
    #
    # 引数:
    # - db_id: 対象Notion DB ID
    #
    # 出力:
    # - 成功: ページ配列(list[dict])
    # - 失敗: 空配列 []
    # ------------------------------------------------------------
    if not db_id:
        return []
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    status, text, data = await notion_request("POST", url, json_body={})
    if status != 200:
        # ログ出力
        logger.error("イベント一覧取得失敗: %s", text)
        return []
    return data.get("results", []) if data else []

# イベント名が除外ワード("定例会")を含むかどうか
def is_ignored_event(name: str) -> bool:
    # ------------------------------------------------------------
    # 同期対象から除外するイベント名かを判定する。
    #
    # 引数:
    # - name: イベント名
    #
    # 出力:
    # - True: 除外対象（「定例会」を含む）
    # - False: 同期対象
    # ------------------------------------------------------------
    return "定例会" in name


def get_event_url(event) -> str:
    # ------------------------------------------------------------
    # Discord Scheduled Event のURLを取得する。
    #
    # 引数:
    # - event: Discord Scheduled Event オブジェクト
    #
    # 出力:
    # - URL文字列
    # - 取得不可の場合は None
    #
    # 優先順位:
    # - event.url があればそれを使う
    # - 無ければ guild_id / event.id からURLを組み立てる
    # ------------------------------------------------------------
    # discord.pyのevent.urlがあればそれを使い、無ければURLを組み立てる
    url = getattr(event, "url", None)
    if url:
        return str(url)
    guild_id = getattr(event, "guild_id", None)
    if guild_id:
        return f"https://discord.com/events/{guild_id}/{event.id}"
    return None


def get_event_location(event) -> str:
    # Discord Scheduled Event の場所情報を取得する。
    # event.location を優先し、無ければ entity_metadata.location を参照する。
    location = getattr(event, "location", None)
    if location:
        text = str(location).strip()
        if text:
            return text
    metadata = getattr(event, "entity_metadata", None)
    meta_location = getattr(metadata, "location", None) if metadata else None
    if meta_location:
        text = str(meta_location).strip()
        if text:
            return text
    return None


def get_google_event_id_from_notion_page(page) -> str:
    # Notionページから GoogleイベントID を取り出す。
    if not page:
        return None
    props = page.get("properties", {})
    rich = props.get("GoogleイベントID", {}).get("rich_text", [])
    if not rich:
        return None
    node = rich[0]
    plain = node.get("plain_text")
    if plain:
        return str(plain).strip() or None
    content = node.get("text", {}).get("content")
    if content:
        return str(content).strip() or None
    return None


def is_bot_created_scheduled_event(event) -> bool:
    # ------------------------------------------------------------
    # Discord Scheduled Event が Bot 自身の作成かを判定する。
    #
    # 引数:
    # - event: Discord Scheduled Event
    #
    # 出力:
    # - True: Bot自身が作成したイベント
    # - False: それ以外
    #
    # 判定順:
    # 1) event.creator_id
    # 2) event.creator.id
    #
    # 備考:
    # Google -> Discord 同期で Bot が作成したイベントを再同期すると
    # ループするため、Discord起点ハンドラ側でスキップ判定に使う。
    # ------------------------------------------------------------
    user = getattr(bot, "user", None)
    if not user:
        return False
    creator_id = getattr(event, "creator_id", None)
    if creator_id is not None and int(creator_id) == int(user.id):
        return True
    creator = getattr(event, "creator", None)
    creator_obj_id = getattr(creator, "id", None) if creator else None
    if creator_obj_id is not None and int(creator_obj_id) == int(user.id):
        return True
    return False


async def find_event_page(db_id, event_id_str):
    # ------------------------------------------------------------
    # Notion DB内から「メッセージID == event_id_str」のページを検索する。
    #
    # 引数:
    # - db_id: 検索対象Notion DB ID
    # - event_id_str: DiscordイベントID（文字列）
    #
    # 出力:
    # - 見つかったページ(dict)
    # - 見つからない場合 None
    #
    # 備考:
    # Discordイベント更新/削除時にNotionページを特定するための関数。
    # ------------------------------------------------------------
    pages = await fetch_event_pages(db_id)
    for page in pages:
        prop = page["properties"].get("メッセージID", {}).get("rich_text", [])
        if not prop:
            continue
        mid = prop[0]["text"]["content"]
        if mid == event_id_str:
            return page
    return None


# ======================================================
# Q&A 機能
# ======================================================

# Q&A DBの取得・差分管理
async def fetch_qa_db():
    # ------------------------------------------------------------
    # Q&A 用 Notion DB をクエリしてページ一覧を取得する。
    #
    # 引数:
    # - なし（NOTION_QA_DB_ID を参照）
    #
    # 出力:
    # - 成功: DBクエリ結果JSON(dict)
    # - 失敗: None
    # ------------------------------------------------------------
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    status, _text, data = await notion_request("POST", url, json_body={})
    return data if status == 200 else None

#ローカルにjsonファイル作成
CACHE_FILE = "notion_cache.json"
REMINDER_CACHE_FILE = "reminder_cache.json"

# キャッシュ読み込み
def load_cache():
    # ------------------------------------------------------------
    # ローカルキャッシュファイル（notion_cache.json）を読み込む。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - 成功: キャッシュdict
    # - 失敗/未作成: 空dict {}
    #
    # 備考:
    # JSON破損や読み込み例外でも空dictにフォールバックする。
    # ------------------------------------------------------------
    if not os.path.exists(CACHE_FILE):
        return {}
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

# キャッシュ書き込み＋初回起動フラグ
def save_cache(cache, first_run_flag=None):
    # ------------------------------------------------------------
    # キャッシュをローカルJSONへ保存する。
    #
    # 引数:
    # - cache: 保存するキャッシュdict
    # - first_run_flag: 任意。初回起動判定フラグ
    #
    # 出力:
    # - なし
    #
    # 備考:
    # first_run_flag が指定された場合は _first_qa_run として同時保存する。
    # ------------------------------------------------------------
    # FIRST_QA_RUN のフラグをキャッシュに保存
    if first_run_flag is not None:
        cache["_first_qa_run"] = first_run_flag

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def load_reminder_cache():
    # ------------------------------------------------------------
    # 前日メンション送信済みイベントのキャッシュを読み込む。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - 成功: キャッシュdict（key=event_id, value=送信時刻ISO）
    # - 失敗/未作成: 空dict {}
    #
    # 備考:
    # JSON破損や想定外型の場合も空dictへフォールバックする。
    # ------------------------------------------------------------
    # 前日メンションの送信済みイベントを保持するキャッシュを読み込む
    if not os.path.exists(REMINDER_CACHE_FILE):
        return {}
    try:
        with open(REMINDER_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_reminder_cache(cache):
    # ------------------------------------------------------------
    # 前日メンション送信済みキャッシュをローカルJSONへ保存する。
    #
    # 引数:
    # - cache: 保存対象のdict
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # 前日メンションの送信済みイベントキャッシュを書き込む
    with open(REMINDER_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

# 新規 / 更新ページの検出
async def get_qa_changes():
    # ------------------------------------------------------------
    # Notion Q&A DB の新規/更新ページを差分検出する。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - 差分配列 list[tuple[str, dict]]
    #   ("new" | "update", page_json)
    #
    # 備考:
    # 比較キーは page_id と last_edited_time。
    # 判定後に最新状態をキャッシュへ保存する。
    # ------------------------------------------------------------
    data = await fetch_qa_db()
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
    # ------------------------------------------------------------
    # Q&Aページから質問文を取り出す。
    #
    # 引数:
    # - page: NotionページJSON
    #
    # 出力:
    # - 質問文字列
    # - 未設定時は "(質問なし)"
    # ------------------------------------------------------------
    t = page["properties"]["質問"]["title"]
    return t[0]["plain_text"] if t else "(質問なし)"


def get_answer(page) -> str:
    # ------------------------------------------------------------
    # Q&Aページから回答文を取り出す。
    #
    # 引数:
    # - page: NotionページJSON
    #
    # 出力:
    # - 回答文字列
    # - 未設定時は "(回答なし)"
    # ------------------------------------------------------------
    t = page["properties"]["回答"]["rich_text"]
    return t[0]["plain_text"] if t else "(回答なし)"

# 未回答の質問一覧
async def fetch_unanswered():
    # ------------------------------------------------------------
    # 回答プロパティが空の質問ページ一覧を取得する。
    #
    # 引数:
    # - なし（NOTION_QA_DB_ID を参照）
    #
    # 出力:
    # - 成功: 未回答ページ配列 list[dict]
    # - 失敗: 空配列 []
    # ------------------------------------------------------------
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    data = {"filter": {"property": "回答", "rich_text": {"is_empty": True}}}
    status, _text, res_data = await notion_request("POST", url, json_body=data)
    # APIリクエストが成功したらjsonを返す
    return res_data.get("results", []) if status == 200 and res_data else []

# 回答済みの質問一覧
async def fetch_answered():
    # ------------------------------------------------------------
    # 回答プロパティが埋まっている質問ページ一覧を取得する。
    #
    # 引数:
    # - なし（NOTION_QA_DB_ID を参照）
    #
    # 出力:
    # - 成功: 回答済みページ配列 list[dict]
    # - 失敗: 空配列 []
    # ------------------------------------------------------------
    url = f"https://api.notion.com/v1/databases/{NOTION_QA_DB_ID}/query"
    data = {"filter": {"property": "回答", "rich_text": {"is_not_empty": True}}}
    status, _text, res_data = await notion_request("POST", url, json_body=data)
    return res_data.get("results", []) if status == 200 and res_data else []

# Notionに回答を書き込む
async def update_answer(page_id, answer: str) -> bool:
    # ------------------------------------------------------------
    # 指定ページの「回答」プロパティを更新する。
    #
    # 引数:
    # - page_id: 更新対象ページID
    # - answer: 保存する回答文
    #
    # 出力:
    # - 成功: True
    # - 失敗: False
    # ------------------------------------------------------------
    url = f"https://api.notion.com/v1/pages/{page_id}"
    data = {
        "properties": {
            "回答": {
                "rich_text": [{"type": "text", "text": {"content": answer}}]
            }
        }
    }
    # 更新リクエストと成功判定
    status, _text, _res_data = await notion_request("PATCH", url, json_body=data)
    return status == 200


async def ensure_question_numbers():
    # ------------------------------------------------------------
    # 質問番号が未採番のページに連番を付与する。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - なし
    #
    # 処理概要:
    # 1) 既存の最大質問番号を取得
    # 2) 未採番ページを created_time 昇順で並べる
    # 3) next_num から順に採番して保存
    # ------------------------------------------------------------
    # 質問番号を持たないページにだけ、追加順で新しい番号を付与する
    data = await fetch_qa_db()
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
        await notion_request("PATCH", url, json_body=data)
        next_num += 1

    # ログ出力
    if missing_pages:
        logger.info("新たに %s 件の質問番号を採番しました。", len(missing_pages))


async def send_qa_notification(bot: commands.Bot, ctype: str, page: dict):
    # ------------------------------------------------------------
    # Q&A の新規/更新通知を指定チャンネルへ送信する。
    #
    # 引数:
    # - bot: Discord Bot インスタンス
    # - ctype: "new" または "update"
    # - page: NotionページJSON
    #
    # 出力:
    # - なし
    #
    # 備考:
    # QA_CHANNEL_ID == 0 の場合は通知をスキップする。
    # ------------------------------------------------------------
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
    # ------------------------------------------------------------
    # 回答者本人だけに見えるエフェメラル通知を送信する。
    #
    # 引数:
    # - interaction: Discord Interaction
    # - number: 質問番号
    # - question: 質問文
    # - answer: 回答文
    # - action: 通知文言
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
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
    # ------------------------------------------------------------
    # 未回答の質問に対して、回答入力を受け付ける Discord モーダル。
    #
    # 役割:
    # - 選択された質問ページIDを保持
    # - 回答入力欄を表示
    # - 送信時に Notion の回答プロパティを更新
    # ------------------------------------------------------------
    #ページID、質問番号、質問文
    def __init__(self, page_id, number, question_text):
        # ------------------------------------------------------------
        # モーダル初期化時に対象ページ情報と入力UIを設定する。
        #
        # 引数:
        # - page_id: 回答を書き込む Notion ページID
        # - number: 質問番号（表示用）
        # - question_text: 質問文（ラベル/再通知用）
        #
        # 出力:
        # - なし（インスタンス状態を初期化）
        # ------------------------------------------------------------
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
        # ------------------------------------------------------------
        # モーダル送信時の処理。
        #
        # 処理概要:
        # 1) update_answer() で Notion の回答を更新
        # 2) 成功時は保存完了メッセージを返す
        # 3) 成功時のみ send_qa_ephemeral() で本人向け再通知
        # 4) 失敗時はエラーメッセージを返す
        #
        # 引数:
        # - interaction: Discord Interaction
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
        # Notionに回答を書き込み、成功時のみ回答者へDM再送
        ok = await update_answer(self.page_id, self.answer.value)
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
    # ------------------------------------------------------------
    # 回答済み質問の回答文を編集するための Discord モーダル。
    #
    # 役割:
    # - 対象質問のページID・質問番号・質問文を保持
    # - 既存回答を初期値として入力欄に表示
    # - 送信時に Notion の回答プロパティを更新
    # ------------------------------------------------------------
    # 回答済みの質問について、回答を編集するモーダル

    def __init__(self, page_id, number, question_text, current_answer):
        # ------------------------------------------------------------
        # 編集モーダルの初期状態を構築する。
        #
        # 引数:
        # - page_id: 更新対象の Notion ページID
        # - number: 質問番号（表示用）
        # - question_text: 質問文（ラベル/再通知用）
        # - current_answer: 現在の回答文（入力初期値）
        #
        # 出力:
        # - なし（インスタンス状態を初期化）
        # ------------------------------------------------------------
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
        # ------------------------------------------------------------
        # 編集モーダル送信時の処理。
        #
        # 処理概要:
        # 1) update_answer() で既存回答を更新
        # 2) 成功時は更新完了メッセージを返す
        # 3) 成功時のみ send_qa_ephemeral() で本人向け再通知
        # 4) 失敗時はエラーメッセージを返す
        #
        # 引数:
        # - interaction: Discord Interaction
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
        # 既存回答を更新し、成功時は回答者へDM再送
        ok = await update_answer(self.page_id, self.answer.value)
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
    # ------------------------------------------------------------
    # 未回答質問の一覧から回答対象を選択するための View。
    #
    # 役割:
    # - 未回答ページをプルダウン候補へ変換
    # - 選択されたページIDから QAnswerModal を起動
    # ------------------------------------------------------------
    # 未回答質問用の番号選択プルダウン

    def __init__(self, pages):
        # ------------------------------------------------------------
        # 未回答質問の選択UI（Select）を構築する。
        #
        # 引数:
        # - pages: 未回答ページ配列(list[dict])
        #
        # 出力:
        # - なし（ViewにSelectを追加）
        #
        # 備考:
        # DiscordのSelect上限に合わせ、候補は最大25件に制限する。
        # ------------------------------------------------------------
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
        # ------------------------------------------------------------
        # プルダウン選択時に呼ばれ、回答入力モーダルを表示する。
        #
        # 引数:
        # - interaction: Discord Interaction（選択結果を含む）
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
        # interaction.data にはSelectの選択結果が入る（max_values=1なので先頭）
        pid = interaction.data["values"][0]
        info = self.page_info[pid]
        number = info["number"]
        question = info["question"]

        await interaction.response.send_modal(
            QAnswerModal(pid, number, question)
        )


class EditSelectView(discord.ui.View):
    # ------------------------------------------------------------
    # 回答済み質問の一覧から編集対象を選択するための View。
    #
    # 役割:
    # - 回答済みページをプルダウン候補へ変換
    # - 選択されたページIDから QEditModal を起動
    # ------------------------------------------------------------
    # 回答済み質問用の番号選択プルダウン

    def __init__(self, pages):
        # ------------------------------------------------------------
        # 回答編集用の選択UI（Select）を構築する。
        #
        # 引数:
        # - pages: 回答済みページ配列(list[dict])
        #
        # 出力:
        # - なし（ViewにSelectを追加）
        #
        # 備考:
        # DiscordのSelect上限に合わせ、候補は最大25件に制限する。
        # ------------------------------------------------------------
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
        # ------------------------------------------------------------
        # プルダウン選択時に呼ばれ、回答編集モーダルを表示する。
        #
        # 引数:
        # - interaction: Discord Interaction（選択結果を含む）
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
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
    # ------------------------------------------------------------
    # Q&A 機能のスラッシュコマンドをまとめた Cog。
    #
    # 役割:
    # - 未回答質問への回答導線（/q_answer）を提供
    # - 回答済み質問の編集導線（/q_edit）を提供
    # ------------------------------------------------------------
    def __init__(self, bot):
        # ------------------------------------------------------------
        # 関数解説:
        # Cog 初期化時に Bot インスタンスを保持する。
        #
        # 引数:
        # - bot: commands.Bot
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
        self.bot = bot
    
    # 回答用コマンドを定義
    @app_commands.command(name="q_answer", description="未回答の質問に回答します")
    async def q_answer(self, interaction: discord.Interaction):
        # ------------------------------------------------------------
        # 未回答質問への回答フローを開始するスラッシュコマンド。
        #
        # 処理概要:
        # 1) 実行チャンネル制約を検証
        # 2) 質問番号を補完
        # 3) 未回答ページを取得
        # 4) AnswerSelectView をエフェメラルで表示
        #
        # 引数:
        # - interaction: Discord Interaction
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
        if not is_qa_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{QA_CHANNEL_ID}> でのみ実行できます。",
                ephemeral=True,
            )
        
        # Bot側で自動連番
        await ensure_question_numbers()
        pages = await fetch_unanswered() # 未回答ページ取得
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
        # ------------------------------------------------------------
        # 回答済み質問の編集フローを開始するスラッシュコマンド。
        #
        # 処理概要:
        # 1) 実行チャンネル制約を検証
        # 2) 質問番号を補完
        # 3) 回答済みページを取得
        # 4) EditSelectView をエフェメラルで表示
        #
        # 引数:
        # - interaction: Discord Interaction
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
        if not is_qa_channel(interaction):
            return await interaction.response.send_message(
                f"❌ このコマンドは <#{QA_CHANNEL_ID}> でのみ実行できます。",
                ephemeral=True,
            )
        # Bot側で自動連番
        await ensure_question_numbers()
        pages = await fetch_answered() # 未回答ページ取得
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


async def send_day_before_reminder(bot: commands.Bot, event) -> bool:
    # ------------------------------------------------------------
    # 指定イベントに対する前日リマインドを Discord へ1件送信する。
    #
    # 引数:
    # - bot: commands.Bot（チャンネル取得/送信に利用）
    # - event: Discord Scheduled Event
    #
    # 出力:
    # - 成功: True
    # - 失敗/スキップ: False
    #
    # 備考:
    # - REMINDER_CHANNEL_ID / REMINDER_ROLE_ID が未設定なら送信しない
    # - allowed_mentions でロールメンションのみ許可する
    # ------------------------------------------------------------
    # 前日メンションを1件送信する
    if REMINDER_CHANNEL_ID == 0 or REMINDER_ROLE_ID == 0:
        return False

    channel = bot.get_channel(REMINDER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(REMINDER_CHANNEL_ID)
        except Exception as exc:
            logger.warning("failed to fetch reminder channel: %s", exc)
            return False

    start_iso = to_jst_iso(event.start_time)
    display_date = format_display_date(start_iso)
    description = event.description or "(内容なし)"
    event_url = get_event_url(event) or ""

    msg = (
        f"🔔 <@&{REMINDER_ROLE_ID}> 明日開催のイベントがあります 🔔\n"
        f"{event_url}"
    )

    try:
        await channel.send(
            msg,
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
        )
        return True
    except Exception as exc:
        logger.warning("failed to send day-before reminder: %s", exc)
        return False


# ======================================================
# 自動タスク
# ======================================================
@tasks.loop(hours=24)
async def auto_clean():
    # ------------------------------------------------------------
    # 関数解説:
    # 定期的にイベントデータの自動クリーンアップを実行するタスク。
    #
    # 実行間隔:
    # - 24時間ごと
    #
    # 処理内容:
    # - delete_past_events() を呼び、
    #   外部/内部DBのポリシーに従って古いイベントをアーカイブする
    #
    # 出力:
    # - なし（副作用で Notion ページをアーカイブ）
    # ------------------------------------------------------------
    # イベントの過去データ削除（24時間毎）
    await delete_past_events()


@tasks.loop(hours=6)
async def auto_check_qa(bot: commands.Bot):
    # ------------------------------------------------------------
    # Q&A DB の変更を定期監視し、通知対象をDiscordへ送信するタスク。
    #
    # 引数:
    # - bot: commands.Bot（通知送信用）
    #
    # 実行間隔:
    # - 6時間ごと
    #
    # 処理概要:
    # 1) 質問番号を補完
    # 2) get_qa_changes() で差分抽出
    # 3) 初回起動時は通知せず、キャッシュ初期化のみ実施
    # 4) 2回目以降は未回答ページのみ通知
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # Q&A DBの変更監視（6時間毎）
    global FIRST_QA_RUN

    await ensure_question_numbers()
    changes = await get_qa_changes()

    # 起動直後は通知せず、キャッシュ作成だけ行う
    if FIRST_QA_RUN:
        logger.info("Skipping QA notifications on first run.")

        # FIRST_QA_RUN = False をキャッシュへ保存
        cache = load_cache()
        save_cache(cache, first_run_flag=False)

        FIRST_QA_RUN = False
        return

    # 2回目以降：未回答のものだけ通知
    for ctype, page in changes:
        if get_answer(page) == "(回答なし)":
            await send_qa_notification(bot, ctype, page)


@tasks.loop(minutes=10)
async def auto_day_before_reminder(bot: commands.Bot):
    # ------------------------------------------------------------
    # 24時間後に開始するイベントを検出し、前日メンションを送信する定期タスク。
    #
    # 引数:
    # - bot: commands.Bot
    #
    # 実行間隔:
    # - 10分ごと
    #
    # 処理概要:
    # 1) 24時間後〜24時間後+ウィンドウの時間帯を計算
    # 2) 各ギルド(サーバー情報)の Scheduled Event を取得
    # 3) 対象イベントを抽出
    # 4) 未送信イベントだけ send_day_before_reminder() を実行
    # 5) 送信成功分をキャッシュ保存
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # Discordイベントの「開始24時間前」を検出し、指定ロールへメンション通知する
    if REMINDER_CHANNEL_ID == 0 or REMINDER_ROLE_ID == 0:
        return

    now_utc = datetime.now(timezone.utc)
    window_start = now_utc + timedelta(hours=24)
    window_end = window_start + timedelta(minutes=max(1, REMINDER_WINDOW_MINUTES))

    cache = load_reminder_cache()
    cache_changed = False

    for guild in bot.guilds:
        try:
            events = await guild.fetch_scheduled_events()
        except Exception:
            events = list(getattr(guild, "scheduled_events", []))

        for event in events:
            start_time = getattr(event, "start_time", None)
            if not start_time:
                continue
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)

            if not (window_start <= start_time < window_end):
                continue

            event_id = str(event.id)
            if event_id in cache:
                continue

            sent = await send_day_before_reminder(bot, event)
            if sent:
                cache[event_id] = now_utc.isoformat()
                cache_changed = True

    if cache_changed:
        save_reminder_cache(cache)


# ======================================================
# Bot 本体
# ======================================================
class MyBot(commands.Bot):
    # ------------------------------------------------------------
    # Bot本体 クラス。
    #
    # 役割:
    # - 起動時フックで Cog とスラッシュコマンドを登録
    # - 既定の commands.Bot を用途に合わせて拡張
    # ------------------------------------------------------------
    # コマンド登録
    async def setup_hook(self):
        # ------------------------------------------------------------
        # Bot 起動時に一度だけ呼ばれる初期化フック。
        #
        # 処理概要:
        # 1) Q&A コマンド Cog を登録
        # 2) スラッシュコマンドを Discord 側へ同期
        #
        # 出力:
        # - なし
        # ------------------------------------------------------------
        await self.add_cog(QACommands(self))
        await self.tree.sync()
        logger.info("Slash commands synced")


intents = discord.Intents.default()
# Discordのイベント機能を使うためのインテント
intents.guild_scheduled_events = True

bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    # ------------------------------------------------------------
    # Bot 接続完了時に実行されるイベントハンドラ。
    #
    # 処理概要:
    # 1) キャッシュから FIRST_QA_RUN を復元
    # 2) 質問番号の欠番補完を実行
    # 3) 定期タスク（auto_clean / auto_check_qa）を起動
    #
    # 出力:
    # - なし
    #
    # 備考:
    # 再接続時の重複起動を避けるため、is_running() で起動状態を確認する。
    # ------------------------------------------------------------
    global FIRST_QA_RUN

    logger.info("Bot Ready as %s", bot.user)

    # FIRST_QA_RUN をキャッシュから復元
    cache = load_cache()
    FIRST_QA_RUN = cache.get("_first_qa_run", True)

    logger.info("FIRST_QA_RUN = %s", FIRST_QA_RUN)
    validate_google_calendar_connection()

    await ensure_question_numbers()

    if not auto_clean.is_running():
        auto_clean.start()

    if not auto_check_qa.is_running():
        auto_check_qa.start(bot)

    if not auto_day_before_reminder.is_running():
        auto_day_before_reminder.start(bot)

    logger.info("All background tasks started.")


# ======================================================
# Discordイベント機能 → Notion同期部分
# ======================================================

@bot.event
async def on_scheduled_event_create(event):
    # ------------------------------------------------------------
    # Discord の Scheduled Event 作成時に呼ばれるイベントハンドラ。
    #
    # 処理概要:
    # 1) Bot自身作成イベントなら同期をスキップ（ループ防止）
    # 2) Discordイベント情報を取得
    # 3) Google Calendar へイベントを作成
    # 4) 外部用Notion DBへ登録（定例会は除外）
    # 5) 内部用Notion DBへ登録（定例会も含む）
    #
    # 引数:
    # - event: Discord Scheduled Event
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    """
    Discord のサーバーイベントが作成されたときに呼ばれる
    ここで Notion のイベントDBに登録する
    """
    name = event.name
    if is_bot_created_scheduled_event(event):
        logger.info("Bot作成イベントのためDiscord->Google/Notion同期をスキップ: %s", name)
        return
    description = event.description or "(内容なし)"
    start_iso = to_jst_iso(event.start_time)
    event_url = get_event_url(event)
    event_location = get_event_location(event)
    creator_id = (
        event.creator_id
        or (event.creator.id if event.creator else "unknown")
    )

    # Discordイベント作成時にGoogleカレンダーへ登録（終了時刻が無い場合は1時間後）
    end_time = event.end_time or (event.start_time + timedelta(hours=1))
    google_event = google_add_event(
        name,
        description,
        event.start_time,
        end_time,
        location=event_location,
    )
    google_event_id = google_event.get("id") if google_event else None

    # 外部用DB: 定例会は除外
    if not is_ignored_event(event.name):
        await notion_add_event(
            NOTION_EVENT_EXTERNAL_DB_ID,
            name=name,
            content=description,
            date_iso=start_iso,
            message_id=event.id,  # メッセージID枠にイベントIDを保存
            creator_id=creator_id,
            google_event_id=google_event_id,
        )
    else:
        logger.warning("外部用DBは除外イベントのため登録しません: %s", event.name)

    # 内部用DB: 定例会も含めて登録（URL/GoogleイベントID付き）
    await notion_add_event(
        NOTION_EVENT_INTERNAL_DB_ID,
        name=name,
        content=description,
        date_iso=start_iso,
        message_id=event.id,  # メッセージID枠にイベントIDを保存
        creator_id=creator_id,
        event_url=event_url,
        google_event_id=google_event_id,
        location=event_location,
    )

    logger.info("Discordイベント作成 -> Notion登録: %s", name)


@bot.event
async def on_scheduled_event_update(before, after):
    # ------------------------------------------------------------
    # Discord の Scheduled Event 更新時に呼ばれるイベントハンドラ。
    #
    # 処理概要:
    # 1) Bot自身作成イベントなら同期をスキップ（ループ防止）
    # 2) after.id をキーに Notion 側の対応ページを検索
    # 3) 外部用DBを更新（定例会は除外）
    # 4) 内部用DBを更新（定例会も含む）
    # 5) 内部DBの GoogleイベントID があれば Google Calendar も更新
    #
    # 引数:
    # - before: 更新前イベント
    # - after: 更新後イベント
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    """
    Discord イベントが更新されたときに呼ばれる
    Notion 側で「メッセージID == after.id」のページを探して更新
    """
    after_id_str = str(after.id)
    if is_bot_created_scheduled_event(after):
        logger.info("Bot作成イベントのためDiscord更新同期をスキップ: %s", after.name)
        return
    event_url = get_event_url(after)

    # 外部用DB: 定例会は除外
    target = None
    if not is_ignored_event(after.name):
        target = await find_event_page(NOTION_EVENT_EXTERNAL_DB_ID, after_id_str)
    else:
        logger.warning("外部用DBは除外イベントのため更新しません: %s", after.name)

    # 内部用DB: 定例会も含めて更新
    internal_target = await find_event_page(NOTION_EVENT_INTERNAL_DB_ID, after_id_str)

    new_name = after.name
    new_content = after.description or "(内容なし)"
    new_date_iso = to_jst_iso(after.start_time)
    new_location = get_event_location(after)
    new_end_time = after.end_time or (after.start_time + timedelta(hours=1))

    # 内部DBに保存した GoogleイベントID があれば、Googleカレンダー側も更新する。
    google_event_id = None
    if internal_target:
        internal_page = await notion_get_event(internal_target["id"])
        google_event_id = get_google_event_id_from_notion_page(internal_page)
    if google_event_id:
        google_updated = google_update_event(
            google_event_id=google_event_id,
            name=new_name,
            description=new_content,
            start_dt=after.start_time,
            end_dt=new_end_time,
            location=new_location,
        )
        if google_updated:
            logger.info("Discordイベント更新 -> Googleカレンダー更新: %s", new_name)
        else:
            logger.error("Googleカレンダー イベント更新に失敗しました。")
    else:
        logger.warning("GoogleイベントIDが見つからないためGoogle更新をスキップします: %s", new_name)

    if target:
        page_id = target["id"]
        ok = await notion_update_event(
            page_id,
            name=new_name,
            content=new_content,
            date_iso=new_date_iso,
        )
        if ok:
            logger.info("Discordイベント更新 -> 外部用Notion更新: %s", new_name)
        else:
            logger.error("外部用Notion イベント更新に失敗しました。")
    else:
        if NOTION_EVENT_EXTERNAL_DB_ID and not is_ignored_event(after.name):
            logger.warning("外部用Notion 側に対応するイベントページが見つかりません。")

    if internal_target:
        page_id = internal_target["id"]
        ok = await notion_update_event(
            page_id,
            name=new_name,
            content=new_content,
            date_iso=new_date_iso,
            event_url=event_url,
            location=new_location,
        )
        if ok:
            logger.info("Discordイベント更新 -> 内部用Notion更新: %s", new_name)
        else:
            logger.error("内部用Notion イベント更新に失敗しました。")
    else:
        if NOTION_EVENT_INTERNAL_DB_ID:
            logger.warning("内部用Notion 側に対応するイベントページが見つかりません。")


@bot.event
async def on_scheduled_event_delete(event):
    # ------------------------------------------------------------
    # Discord の Scheduled Event 削除時に呼ばれるイベントハンドラ。
    #
    # 処理概要:
    # 1) Bot自身作成イベントなら同期をスキップ（ループ防止）
    # 2) event.id をキーに Notion 側の対応ページを検索
    # 3) 外部用DBをアーカイブ（定例会は除外）
    # 4) 内部用DBをアーカイブ（定例会も含む）
    # 5) 内部DBの GoogleイベントID があれば Google Calendar も削除
    #
    # 引数:
    # - event: Discord Scheduled Event
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    """
    Discord イベントが削除されたときに呼ばれる
    Notion 側の対応するページをアーカイブし、必要に応じてGoogle側も削除
    """
    eid = str(event.id)
    if is_bot_created_scheduled_event(event):
        logger.info("Bot作成イベントのためDiscord削除同期をスキップ: %s", event.name)
        return

    # 外部用DB: 定例会は除外
    if not is_ignored_event(event.name):
        target = await find_event_page(NOTION_EVENT_EXTERNAL_DB_ID, eid)
        if target:
            if await notion_delete_event(target["id"]):
                logger.info("Discordイベント削除 -> 外部用Notion削除: %s", event.name)
            else:
                logger.error("外部用Notion イベント削除に失敗しました。")
        else:
            if NOTION_EVENT_EXTERNAL_DB_ID:
                logger.warning("外部用の削除対象Notionイベントが見つかりません。")
    else:
        logger.warning("外部用DBは除外イベントの削除は無視します: %s", event.name)

    # 内部用DB: 定例会も含めて削除
    internal_target = await find_event_page(NOTION_EVENT_INTERNAL_DB_ID, eid)
    if internal_target:
        internal_page = await notion_get_event(internal_target["id"])
        google_event_id = get_google_event_id_from_notion_page(internal_page)
        if google_event_id:
            deleted = google_delete_event(google_event_id)
            if deleted:
                logger.info("Discordイベント削除 -> Googleカレンダー削除: %s", event.name)
            else:
                logger.error("Googleカレンダー イベント削除に失敗しました。")
        else:
            logger.warning("GoogleイベントIDが見つからないためGoogle削除をスキップします: %s", event.name)
        if await notion_delete_event(internal_target["id"]):
            logger.info("Discordイベント削除 -> 内部用Notion削除: %s", event.name)
        else:
            logger.error("内部用Notion イベント削除に失敗しました。")
    else:
        if NOTION_EVENT_INTERNAL_DB_ID:
            logger.warning("内部用の削除対象Notionイベントが見つかりません。")


# ===============================
# Bot 起動
# ===============================
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

