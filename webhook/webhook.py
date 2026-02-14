import json
import logging
import os
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
import threading
import time
from flask import Flask, request
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("webhook")

app = Flask(__name__)


def getenv_clean(name: str, default=None):
    # ------------------------------------------------------------
    # 環境変数を取得し、文字列なら前後空白を除去して返す。
    #
    # 引数:
    # - name: 環境変数名
    # - default: 未設定時のデフォルト値
    #
    # 出力:
    # - 文字列: strip後の値（空文字なら default）
    # - 非文字列: そのまま
    # ------------------------------------------------------------
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
        return value if value else default
    return value


NOTION_TOKEN = getenv_clean("NOTION_TOKEN")
NOTION_EVENT_INTERNAL_DB_ID = getenv_clean("NOTION_EVENT_INTERNAL_ID")
NOTION_EVENT_EXTERNAL_DB_ID = getenv_clean("NOTION_EVENT_ID")

GOOGLE_CALENDAR_ID = getenv_clean("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")

DISCORD_TOKEN = getenv_clean("DISCORD_TOKEN")
DISCORD_GUILD_ID = getenv_clean("DISCORD_GUILD_ID")
DISCORD_SYNC_ENABLED = getenv_clean("DISCORD_SYNC_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
STATE_DIR = getenv_clean("STATE_DIR", ".")
SYNC_STATE_FILE = os.path.join(STATE_DIR, "gcal_sync_state.json")
DEDUPE_STATE_FILE = os.path.join(STATE_DIR, "gcal_recent_messages.json")
GCAL_DISCORD_MAP_FILE = os.path.join(STATE_DIR, "gcal_discord_map.json")
GCAL_NOTION_MAP_FILE = os.path.join(STATE_DIR, "gcal_notion_map.json")
DEDUPE_MAX_IDS = int(getenv_clean("DEDUPE_MAX_IDS", "1000"))
SYNC_COOLDOWN_SECONDS = float(getenv_clean("SYNC_COOLDOWN_SECONDS", "2"))

NOTION_PROP_TITLE = getenv_clean("NOTION_PROP_TITLE", "イベント名")
NOTION_PROP_CONTENT = getenv_clean("NOTION_PROP_CONTENT", "内容")
NOTION_PROP_DATE = getenv_clean("NOTION_PROP_DATE", "日時")
NOTION_PROP_MESSAGE_ID = getenv_clean("NOTION_PROP_MESSAGE_ID", "メッセージID")
NOTION_PROP_CREATOR_ID = getenv_clean("NOTION_PROP_CREATOR_ID", "作成者ID")
NOTION_PROP_PAGE_ID = getenv_clean("NOTION_PROP_PAGE_ID", "ページID")
NOTION_PROP_EVENT_URL = getenv_clean("NOTION_PROP_EVENT_URL", "イベントURL")
NOTION_PROP_GOOGLE_EVENT_ID = getenv_clean("NOTION_PROP_GOOGLE_EVENT_ID", "GoogleイベントID")

DISCORD_DESCRIPTION_LIMIT = int(getenv_clean("DISCORD_DESCRIPTION_LIMIT", "1000"))
DISCORD_NAME_LIMIT = int(getenv_clean("DISCORD_NAME_LIMIT", "100"))
DISCORD_LOCATION_LIMIT = int(getenv_clean("DISCORD_LOCATION_LIMIT", "100"))
DISCORD_LOCATION_FALLBACK = getenv_clean("DISCORD_LOCATION_FALLBACK", "Google Calendar")
DISCORD_ORIGIN_MARKER_PREFIX = "[gcal-id:"
DISCORD_APPEND_GCAL_MARKER = getenv_clean("DISCORD_APPEND_GCAL_MARKER", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_calendar_service = None
_processed_message_ids = deque(maxlen=max(100, DEDUPE_MAX_IDS))
_processed_message_set = set()
_gcal_discord_map = {}
_gcal_notion_map = {"internal": {}, "external": {}}
_sync_lock = threading.Lock()
_sync_last_run_epoch = 0.0


def parse_rfc3339(value):
    # ------------------------------------------------------------
    # RFC3339文字列を datetime へ変換する。
    #
    # 引数:
    # - value: 日時文字列
    #
    # 出力:
    # - 成功: datetime
    # - 失敗: None
    # ------------------------------------------------------------
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def ensure_state_dir():
    # ------------------------------------------------------------
    # 状態ファイル保存先ディレクトリを作成する。
    #
    # 出力:
    # - なし（失敗時はログ出力）
    # ------------------------------------------------------------
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception as exc:
        logger.error("Failed to create STATE_DIR=%s: %s", STATE_DIR, exc)


def load_recent_message_ids():
    # ------------------------------------------------------------
    # 重複排除用の直近メッセージIDを状態ファイルから復元する。
    #
    # 出力:
    # - なし（メモリ上の dedupe 構造に反映）
    # ------------------------------------------------------------
    if not os.path.exists(DEDUPE_STATE_FILE):
        return
    try:
        with open(DEDUPE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ids = data.get("ids", [])
        for mid in ids[-_processed_message_ids.maxlen :]:
            if mid not in _processed_message_set:
                _processed_message_ids.append(mid)
                _processed_message_set.add(mid)
    except Exception as exc:
        logger.warning("Failed to load dedupe state: %s", exc)


def save_recent_message_ids():
    # ------------------------------------------------------------
    # 重複排除用の直近メッセージIDを状態ファイルへ保存する。
    #
    # 出力:
    # - なし（失敗時はログ出力）
    # ------------------------------------------------------------
    ensure_state_dir()
    try:
        with open(DEDUPE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ids": list(_processed_message_ids)}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save dedupe state: %s", exc)


def register_message_id(message_id):
    # ------------------------------------------------------------
    # メッセージIDを重複排除セットへ登録する。
    #
    # 引数:
    # - message_id: 判定対象メッセージID
    #
    # 出力:
    # - True: 既登録（重複）
    # - False: 新規登録
    # ------------------------------------------------------------
    if not message_id:
        return False
    if message_id in _processed_message_set:
        return True
    if len(_processed_message_ids) == _processed_message_ids.maxlen:
        oldest = _processed_message_ids[0]
        _processed_message_set.discard(oldest)
    _processed_message_ids.append(message_id)
    _processed_message_set.add(message_id)
    save_recent_message_ids()
    return False


def load_gcal_discord_map():
    # ------------------------------------------------------------
    # GoogleイベントID -> DiscordイベントID の対応表を読み込む。
    #
    # 出力:
    # - なし（_gcal_discord_map を更新）
    # ------------------------------------------------------------
    global _gcal_discord_map
    if not os.path.exists(GCAL_DISCORD_MAP_FILE):
        _gcal_discord_map = {}
        return
    try:
        with open(GCAL_DISCORD_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("map", {})
        _gcal_discord_map = {str(k): str(v) for k, v in raw.items() if k and v}
    except Exception as exc:
        logger.warning("Failed to load gcal_discord_map: %s", exc)
        _gcal_discord_map = {}


def save_gcal_discord_map():
    # ------------------------------------------------------------
    # GoogleイベントID -> DiscordイベントID の対応表を保存する。
    #
    # 出力:
    # - なし（失敗時はログ出力）
    # ------------------------------------------------------------
    ensure_state_dir()
    try:
        with open(GCAL_DISCORD_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump({"map": _gcal_discord_map}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save gcal_discord_map: %s", exc)


def load_gcal_notion_map():
    # GoogleイベントID -> NotionページID(内部/外部) の対応表を読み込む。
    global _gcal_notion_map
    if not os.path.exists(GCAL_NOTION_MAP_FILE):
        _gcal_notion_map = {"internal": {}, "external": {}}
        return
    try:
        with open(GCAL_NOTION_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        internal = data.get("internal", {}) or {}
        external = data.get("external", {}) or {}
        _gcal_notion_map = {
            "internal": {str(k): str(v) for k, v in internal.items() if k and v},
            "external": {str(k): str(v) for k, v in external.items() if k and v},
        }
    except Exception as exc:
        logger.warning("Failed to load gcal_notion_map: %s", exc)
        _gcal_notion_map = {"internal": {}, "external": {}}


def save_gcal_notion_map():
    # GoogleイベントID -> NotionページID(内部/外部) の対応表を保存する。
    ensure_state_dir()
    try:
        with open(GCAL_NOTION_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(_gcal_notion_map, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save gcal_notion_map: %s", exc)


def get_notion_page_id_by_google_id(google_event_id, scope):
    # scope: internal|external
    if not google_event_id or scope not in ("internal", "external"):
        return None
    return _gcal_notion_map.get(scope, {}).get(str(google_event_id))


def set_notion_page_id_by_google_id(google_event_id, page_id, scope):
    # scope: internal|external
    if not google_event_id or not page_id or scope not in ("internal", "external"):
        return
    _gcal_notion_map.setdefault(scope, {})[str(google_event_id)] = str(page_id)
    save_gcal_notion_map()


def remove_notion_page_id_by_google_id(google_event_id, scope):
    # scope: internal|external
    if not google_event_id or scope not in ("internal", "external"):
        return
    _gcal_notion_map.setdefault(scope, {}).pop(str(google_event_id), None)
    save_gcal_notion_map()


def get_discord_event_id_by_google_id(google_event_id):
    # ------------------------------------------------------------
    # GoogleイベントIDからDiscordイベントIDを取得する。
    #
    # 引数:
    # - google_event_id: GoogleイベントID
    #
    # 出力:
    # - 対応するDiscordイベントID(str)
    # - 未登録: None
    # ------------------------------------------------------------
    if not google_event_id:
        return None
    return _gcal_discord_map.get(str(google_event_id))


def set_discord_event_id_by_google_id(google_event_id, discord_event_id):
    # ------------------------------------------------------------
    # GoogleイベントIDとDiscordイベントIDの対応を保存する。
    #
    # 引数:
    # - google_event_id: GoogleイベントID
    # - discord_event_id: DiscordイベントID
    # ------------------------------------------------------------
    if not google_event_id or not discord_event_id:
        return
    _gcal_discord_map[str(google_event_id)] = str(discord_event_id)
    save_gcal_discord_map()


def remove_discord_event_id_by_google_id(google_event_id):
    # ------------------------------------------------------------
    # GoogleイベントIDに対応するDiscordイベントIDを削除する。
    #
    # 引数:
    # - google_event_id: GoogleイベントID
    # ------------------------------------------------------------
    if not google_event_id:
        return
    _gcal_discord_map.pop(str(google_event_id), None)
    save_gcal_discord_map()


def load_service_account_info():
    # ------------------------------------------------------------
    # Google Service Account 情報を環境変数またはファイルから読み込む。
    #
    # 入力:
    # - GOOGLE_SERVICE_ACCOUNT_JSON:
    #   1) JSON文字列
    #   2) JSONファイルパス
    # - GOOGLE_SERVICE_ACCOUNT_JSON_PATH:
    #   明示的なJSONファイルパス
    #
    # 出力:
    # - 成功: service_account_info(dict)
    # - 失敗: None
    # ------------------------------------------------------------
    json_env = GOOGLE_SERVICE_ACCOUNT_JSON
    if json_env:
        if os.path.exists(json_env):
            try:
                with open(json_env, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.error("Service Account JSON read error(path): %s", exc)
                return None
        try:
            return json.loads(json_env)
        except json.JSONDecodeError:
            logger.error("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON/path")
            return None

    if GOOGLE_SERVICE_ACCOUNT_JSON_PATH:
        if not os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON_PATH):
            logger.error(
                "GOOGLE_SERVICE_ACCOUNT_JSON_PATH not found: %s",
                GOOGLE_SERVICE_ACCOUNT_JSON_PATH,
            )
            return None
        try:
            with open(GOOGLE_SERVICE_ACCOUNT_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.error("Service Account JSON read error(path): %s", exc)
            return None

    logger.warning("Google credentials are not configured")
    return None


def get_calendar_service():
    # ------------------------------------------------------------
    # Google Calendar API service を初期化して返す。
    #
    # 出力:
    # - 成功: googleapiclient service
    # - 失敗: None
    #
    # 備考:
    # - 初期化済み service は _calendar_service を再利用する
    # ------------------------------------------------------------
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service
    if not GOOGLE_CALENDAR_ID:
        logger.error("GOOGLE_CALENDAR_ID is not set")
        return None
    info = load_service_account_info()
    if not info:
        return None
    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        _calendar_service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return _calendar_service
    except Exception as exc:
        logger.error("Google Calendar service init failed: %s", exc)
        return None


def load_sync_state():
    # ------------------------------------------------------------
    # 同期カーソル（updated_min）を状態ファイルから読み込む。
    #
    # 出力:
    # - 成功: 状態dict
    # - 失敗/未作成: {}
    # ------------------------------------------------------------
    if not os.path.exists(SYNC_STATE_FILE):
        return {}
    try:
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sync_state(updated_min):
    # ------------------------------------------------------------
    # 同期カーソル（updated_min）を状態ファイルへ保存する。
    #
    # 引数:
    # - updated_min: 次回差分取得に使うカーソル
    # ------------------------------------------------------------
    ensure_state_dir()
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_min": updated_min}, f, ensure_ascii=False, indent=2)


def list_updated_events(updated_min):
    # ------------------------------------------------------------
    # Google Calendar の更新イベント一覧を取得する。
    #
    # 引数:
    # - updated_min: 差分取得カーソル
    #
    # 出力:
    # - 成功: events(list[dict])
    # - 失敗: []
    #
    # 備考:
    # - 初回は30日lookback
    # - 取りこぼし防止で2分巻き戻し
    # - 410(updatedMinTooLongAgo)時は full fetch にフォールバック
    # ------------------------------------------------------------
    service = get_calendar_service()
    if not service or not GOOGLE_CALENDAR_ID:
        return []

    if not updated_min:
        lookback = datetime.now(timezone.utc) - timedelta(days=30)
        updated_min = lookback.isoformat()
    else:
        dt = parse_rfc3339(updated_min)
        if dt is not None:
            updated_min = (dt - timedelta(minutes=2)).isoformat()

    events = []
    page_token = None

    def fetch_all_without_updated_min():
        logger.warning("Retrying without updatedMin due to 410 updatedMinTooLongAgo")
        all_events = []
        token = None
        while True:
            resp = (
                service.events()
                .list(
                    calendarId=GOOGLE_CALENDAR_ID,
                    singleEvents=True,
                    showDeleted=True,
                    maxResults=2500,
                    pageToken=token,
                )
                .execute()
            )
            all_events.extend(resp.get("items", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return all_events

    try:
        while True:
            resp = (
                service.events()
                .list(
                    calendarId=GOOGLE_CALENDAR_ID,
                    updatedMin=updated_min,
                    singleEvents=True,
                    showDeleted=True,
                    maxResults=2500,
                    pageToken=page_token,
                )
                .execute()
            )
            events.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        if "updatedMinTooLongAgo" in str(exc) or " 410 " in str(exc):
            try:
                return fetch_all_without_updated_min()
            except Exception as retry_exc:
                logger.error("Google event list retry failed: %s", retry_exc)
                return []
        logger.error("Google event list failed: %s", exc)
        return []

    return events


def build_notion_date(event):
    # ------------------------------------------------------------
    # Googleイベントから Notion date プロパティ形式を作成する。
    #
    # 引数:
    # - event: Google Calendar event(dict)
    #
    # 出力:
    # - 成功: {"start": ..., "end": ...}
    # - 失敗: None
    # ------------------------------------------------------------
    start = event.get("start", {})
    end = event.get("end", {})
    start_iso = start.get("dateTime") or start.get("date")
    end_iso = end.get("dateTime") or end.get("date")
    if not start_iso:
        return None
    date_prop = {"start": start_iso}
    if end_iso:
        date_prop["end"] = end_iso
    return date_prop


def notion_extract_rich_text(page, prop_name):
    # ------------------------------------------------------------
    # Notionの rich_text プロパティ先頭テキストを抽出する。
    #
    # 引数:
    # - page: Notionページ(dict)
    # - prop_name: プロパティ名
    #
    # 出力:
    # - 成功: 文字列
    # - 失敗/空: None
    # ------------------------------------------------------------
    if not page:
        return None
    props = page.get("properties", {})
    rich = props.get(prop_name, {}).get("rich_text", [])
    if not rich:
        return None
    node = rich[0]
    plain = node.get("plain_text")
    if plain:
        text = str(plain).strip()
        return text or None
    content = node.get("text", {}).get("content")
    if content:
        text = str(content).strip()
        return text or None
    return None


def notion_find_by_google_event_id(google_event_id, db_id=None):
    # ------------------------------------------------------------
    # GoogleイベントIDで Notion DB から対応ページを1件検索する。
    #
    # 引数:
    # - google_event_id: GoogleイベントID
    # - db_id: 検索対象DB（未指定時は内部DB）
    #
    # 出力:
    # - 見つかったページ(dict)
    # - 未検出/失敗: None
    # ------------------------------------------------------------
    target_db_id = db_id or NOTION_EVENT_INTERNAL_DB_ID
    if not target_db_id:
        logger.error("target notion database id is not set")
        return None

    url = f"https://api.notion.com/v1/databases/{target_db_id}/query"
    data = {
        "filter": {
            "property": NOTION_PROP_GOOGLE_EVENT_ID,
            "rich_text": {"equals": google_event_id},
        }
    }
    res = requests.post(url, headers=headers, json=data, timeout=30)
    if res.status_code != 200:
        logger.error("Notion query error: %s", res.text)
        return None
    results = res.json().get("results", [])
    return results[0] if results else None


def notion_find_by_message_id(message_id, db_id):
    # 指定DBで「メッセージID == message_id」のページを1件検索する。
    if not db_id or not message_id:
        return None
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    data = {
        "filter": {
            "property": NOTION_PROP_MESSAGE_ID,
            "rich_text": {"equals": str(message_id)},
        }
    }
    res = requests.post(url, headers=headers, json=data, timeout=30)
    if res.status_code != 200:
        logger.error("Notion query by message_id error: %s", res.text)
        return None
    results = res.json().get("results", [])
    return results[0] if results else None


def notion_get_page(page_id):
    # ページIDで Notion ページを1件取得する。
    if not page_id:
        return None
    res = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=headers, timeout=30)
    if res.status_code != 200:
        return None
    data = res.json()
    return data if data.get("id") else None


def notion_update_event(
    page_id,
    name=None,
    content=None,
    date_prop=None,
    event_url=None,
    google_event_id=None,
    page_uuid=None,
    message_id=None,
    location=None,
):
    # ------------------------------------------------------------
    # Notionページを部分更新する（None以外の項目だけ更新）。
    #
    # 引数:
    # - page_id: 更新対象ページID
    # - name/content/date_prop/event_url/google_event_id/page_uuid/message_id/location:
    #   None 以外の項目を更新
    #
    # 出力:
    # - 成功: True
    # - 失敗: False
    # ------------------------------------------------------------
    props = {}
    if name is not None:
        props[NOTION_PROP_TITLE] = {"title": [{"text": {"content": name}}]}
    if content is not None:
        props[NOTION_PROP_CONTENT] = {"rich_text": [{"text": {"content": content}}]}
    if date_prop is not None:
        props[NOTION_PROP_DATE] = {"date": date_prop}
    if event_url is not None:
        props[NOTION_PROP_EVENT_URL] = {"url": event_url}
    if google_event_id is not None:
        props[NOTION_PROP_GOOGLE_EVENT_ID] = {
            "rich_text": [{"text": {"content": str(google_event_id)}}]
        }
    if page_uuid is not None:
        props[NOTION_PROP_PAGE_ID] = {"rich_text": [{"text": {"content": str(page_uuid)}}]}
    if message_id is not None:
        props[NOTION_PROP_MESSAGE_ID] = {
            "rich_text": [{"text": {"content": str(message_id)}}]
        }
    if location is not None:
        props["場所"] = {
            "rich_text": [{"text": {"content": str(location)}}]
        }

    res = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=headers,
        json={"properties": props},
        timeout=30,
    )
    if res.status_code not in (200, 201):
        logger.error("Notion update error(page_id=%s): %s", page_id, res.text)
        return False
    return True


def notion_create_event(
    name,
    content,
    date_prop,
    creator_id,
    event_url,
    google_event_id,
    location=None,
    db_id=None,
    message_id=None,
):
    # ------------------------------------------------------------
    # Notion DB にイベントページを新規作成する。
    #
    # 引数:
    # - name/content/date_prop/creator_id/event_url/google_event_id/location
    # - db_id: 作成先DB（未指定時は内部DB）
    #
    # 出力:
    # - 成功: 作成ページID(str)
    # - 失敗: None
    # ------------------------------------------------------------
    url = "https://api.notion.com/v1/pages"
    target_db_id = db_id or NOTION_EVENT_INTERNAL_DB_ID
    data = {
        "parent": {"database_id": target_db_id},
        "properties": {
            NOTION_PROP_TITLE: {"title": [{"text": {"content": name}}]},
            NOTION_PROP_CONTENT: {"rich_text": [{"text": {"content": content}}]},
            NOTION_PROP_DATE: {"date": date_prop},
            NOTION_PROP_MESSAGE_ID: {
                "rich_text": [{"text": {"content": str(message_id) if message_id is not None else ""}}]
            },
            NOTION_PROP_CREATOR_ID: {"rich_text": [{"text": {"content": str(creator_id)}}]},
            NOTION_PROP_PAGE_ID: {"rich_text": [{"text": {"content": ""}}]},
        },
    }
    if event_url is not None:
        data["properties"][NOTION_PROP_EVENT_URL] = {"url": event_url}
    if google_event_id is not None:
        data["properties"][NOTION_PROP_GOOGLE_EVENT_ID] = {
            "rich_text": [{"text": {"content": str(google_event_id)}}]
        }
    if location is not None:
        data["properties"]["場所"] = {
            "rich_text": [{"text": {"content": str(location)}}]
        }
    res = requests.post(url, headers=headers, json=data, timeout=30)
    if res.status_code not in (200, 201):
        logger.error("Notion create error: %s", res.text)
        return None

    page_id = res.json()["id"]
    notion_update_event(page_id, page_uuid=page_id)
    return page_id


def notion_archive_page(page):
    # ------------------------------------------------------------
    # Notionページをアーカイブする。
    #
    # 引数:
    # - page: Notionページ(dict)
    #
    # 出力:
    # - 成功: True
    # - 失敗: False
    # ------------------------------------------------------------
    if not page:
        return False
    res = requests.patch(
        f"https://api.notion.com/v1/pages/{page['id']}",
        headers=headers,
        json={"archived": True},
        timeout=30,
    )
    if res.status_code not in (200, 201):
        logger.error("Notion archive error(page_id=%s): %s", page["id"], res.text)
        return False
    return True


def discord_sync_available():
    # ------------------------------------------------------------
    # Discord同期の実行可否（env設定）を判定する。
    #
    # 出力:
    # - True: 同期可能
    # - False: 同期不可
    # ------------------------------------------------------------
    if not DISCORD_SYNC_ENABLED:
        return False
    if not DISCORD_TOKEN:
        logger.warning("Discord sync disabled: DISCORD_TOKEN is not set")
        return False
    if not DISCORD_GUILD_ID:
        logger.warning("Discord sync disabled: DISCORD_GUILD_ID is not set")
        return False
    return True


def discord_api_request(method, path, payload=None):
    # ------------------------------------------------------------
    # Discord REST API 共通呼び出し。
    #
    # 引数:
    # - method: HTTPメソッド
    # - path: /api/v10 以降のパス
    # - payload: リクエストJSON（任意）
    #
    # 出力:
    # - 成功(204): {}
    # - 成功(JSON): dict
    # - 失敗: None
    # ------------------------------------------------------------
    url = f"https://discord.com/api/v10{path}"
    d_headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }
    res = requests.request(method, url, headers=d_headers, json=payload, timeout=30)
    if res.status_code >= 400:
        logger.error(
            "Discord API error method=%s path=%s status=%s body=%s",
            method,
            path,
            res.status_code,
            res.text,
        )
        return None
    if res.status_code == 204 or not res.text:
        return {}
    try:
        return res.json()
    except Exception:
        return {}


def parse_google_event_times(event):
    # ------------------------------------------------------------
    # Googleイベントの開始/終了時刻をDiscord向けdatetimeへ正規化する。
    #
    # 引数:
    # - event: Google Calendar event(dict)
    #
    # 出力:
    # - 成功: (start_dt, end_dt)
    # - 失敗: (None, None)
    #
    # 備考:
    # - 終了未指定や異常値は +1時間で補正
    # ------------------------------------------------------------
    def parse_part(part, is_end=False):
        date_time = part.get("dateTime")
        date_only = part.get("date")
        if date_time:
            dt = parse_rfc3339(date_time)
            if dt:
                return dt
        if date_only:
            try:
                d = datetime.strptime(date_only, "%Y-%m-%d")
                base = d.replace(tzinfo=timezone(timedelta(hours=9)))
                return base + (timedelta(hours=1) if is_end else timedelta(hours=9))
            except Exception:
                return None
        return None

    start_dt = parse_part(event.get("start", {}), is_end=False)
    end_dt = parse_part(event.get("end", {}), is_end=True)
    if not start_dt:
        return None, None
    if not end_dt or end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)
    return start_dt, end_dt


def to_discord_iso(dt):
    # Discord API 用に UTC ISO8601 (Z) 文字列へ変換する。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_discord_description(description, google_event_id):
    # ------------------------------------------------------------
    # Discord説明文を生成する。
    #
    # 引数:
    # - description: 説明文
    # - google_event_id: GoogleイベントID
    #
    # 出力:
    # - Discord説明文（文字数制限適用済み）
    #
    # 備考:
    # - DISCORD_APPEND_GCAL_MARKER=true の時のみ marker を付与
    # ------------------------------------------------------------
    base = (description or "").strip()
    if DISCORD_APPEND_GCAL_MARKER:
        marker = f"{DISCORD_ORIGIN_MARKER_PREFIX}{google_event_id}]"
        text = f"{base}\n\n{marker}" if base else marker
    else:
        text = base
    return text[:DISCORD_DESCRIPTION_LIMIT]


def build_discord_payload(event):
    # ------------------------------------------------------------
    # Googleイベントから Discord Scheduled Event 用payloadを作成する。
    #
    # 引数:
    # - event: Google Calendar event(dict)
    #
    # 出力:
    # - 成功: payload(dict)
    # - 失敗: None
    # ------------------------------------------------------------
    google_event_id = event.get("id")
    if not google_event_id:
        return None
    start_dt, end_dt = parse_google_event_times(event)
    if not start_dt:
        return None
    location = (event.get("location") or DISCORD_LOCATION_FALLBACK).strip()
    return {
        "name": (event.get("summary") or "(no title)")[:DISCORD_NAME_LIMIT],
        "description": build_discord_description(event.get("description"), google_event_id),
        "privacy_level": 2,
        "entity_type": 3,
        "scheduled_start_time": to_discord_iso(start_dt),
        "scheduled_end_time": to_discord_iso(end_dt),
        "entity_metadata": {"location": location[:DISCORD_LOCATION_LIMIT]},
    }


def discord_create_event(event):
    # Googleイベントを元に Discord Scheduled Event を新規作成する。
    payload = build_discord_payload(event)
    if not payload:
        return None
    return discord_api_request("POST", f"/guilds/{DISCORD_GUILD_ID}/scheduled-events", payload=payload)


def discord_update_event(discord_event_id, event):
    # 指定Discordイベントを Googleイベント内容で更新する。
    payload = build_discord_payload(event)
    if not payload:
        return None
    return discord_api_request(
        "PATCH",
        f"/guilds/{DISCORD_GUILD_ID}/scheduled-events/{discord_event_id}",
        payload=payload,
    )


def discord_delete_event(discord_event_id):
    # 指定Discordイベントを削除する。
    res = discord_api_request("DELETE", f"/guilds/{DISCORD_GUILD_ID}/scheduled-events/{discord_event_id}")
    return res is not None


def find_discord_event_id_by_google_marker(google_event_id):
    # ------------------------------------------------------------
    # GoogleイベントIDの marker から既存DiscordイベントIDを検索する。
    #
    # 引数:
    # - google_event_id: GoogleイベントID
    #
    # 出力:
    # - 見つかったDiscordイベントID(str)
    # - 見つからない/無効時: None
    # ------------------------------------------------------------
    if not DISCORD_APPEND_GCAL_MARKER:
        return None
    if not discord_sync_available() or not google_event_id:
        return None
    marker = f"{DISCORD_ORIGIN_MARKER_PREFIX}{google_event_id}]"
    d_headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
    }
    url = f"https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/scheduled-events"
    res = requests.get(url, headers=d_headers, timeout=30)
    if res.status_code >= 400:
        logger.warning("Discord list events failed status=%s body=%s", res.status_code, res.text)
        return None
    try:
        items = res.json() or []
    except Exception:
        return None
    for item in items:
        description = str(item.get("description") or "")
        if marker in description:
            found = item.get("id")
            if found:
                return str(found)
    return None


def sync_to_discord(event, notion_page, fallback_notion_page=None):
    # ------------------------------------------------------------
    # Googleイベントを Discord Scheduled Event へ同期する。
    #
    # 引数:
    # - event: Google Calendar event(dict)
    # - notion_page: 対応Notionページ(dict|None)
    #
    # 出力:
    # - create/update成功時: DiscordイベントID(str)
    # - それ以外: None
    #
    # 処理概要:
    # 1) 対応するDiscordイベントIDを解決
    # 2) cancelled なら削除
    # 3) 既存IDがあれば更新
    # 4) なければ新規作成
    # ------------------------------------------------------------
    if not discord_sync_available():
        return None
    google_event_id = event.get("id")
    if not google_event_id:
        return None

    notion_discord_id = notion_extract_rich_text(notion_page, NOTION_PROP_MESSAGE_ID)
    if not notion_discord_id and fallback_notion_page:
        notion_discord_id = notion_extract_rich_text(
            fallback_notion_page,
            NOTION_PROP_MESSAGE_ID,
        )
    mapped_discord_id = get_discord_event_id_by_google_id(google_event_id)
    discord_event_id = notion_discord_id or mapped_discord_id

    if not discord_event_id:
        discovered = find_discord_event_id_by_google_marker(google_event_id)
        if discovered:
            discord_event_id = discovered
            set_discord_event_id_by_google_id(google_event_id, discovered)

    if event.get("status") == "cancelled":
        if discord_event_id:
            if discord_delete_event(discord_event_id):
                logger.info(
                    "Discord event deleted by Google cancel: google_event_id=%s discord_event_id=%s",
                    google_event_id,
                    discord_event_id,
                )
            else:
                logger.warning(
                    "Discord delete failed by Google cancel: google_event_id=%s discord_event_id=%s",
                    google_event_id,
                    discord_event_id,
                )
        else:
            logger.warning(
                "Discord delete skipped: discord_event_id unresolved google_event_id=%s",
                google_event_id,
            )
        remove_discord_event_id_by_google_id(google_event_id)
        return None

    if discord_event_id:
        updated = discord_update_event(discord_event_id, event)
        if updated is not None:
            resolved_id = str(updated.get("id") or discord_event_id)
            set_discord_event_id_by_google_id(google_event_id, resolved_id)
            return resolved_id
        logger.warning(
            "Discord update failed for existing event_id=%s; skip create to avoid duplication",
            discord_event_id,
        )
        return None

    created = discord_create_event(event)
    if created and created.get("id"):
        resolved_id = str(created["id"])
        set_discord_event_id_by_google_id(google_event_id, resolved_id)
        logger.info(
            "Discord event created from Google: google_event_id=%s discord_event_id=%s",
            google_event_id,
            resolved_id,
        )
        return resolved_id
    return None


def upsert_event(event):
    # ------------------------------------------------------------
    # Googleイベント1件を Notion / Discord に反映する。
    #
    # 引数:
    # - event: Google Calendar event(dict)
    #
    # 出力:
    # - なし
    #
    # 処理概要:
    # - cancelled: Notionアーカイブ + Discord削除
    # - active: Notion(内部/外部) upsert + Discord create/update
    # ------------------------------------------------------------
    google_event_id = event.get("id")
    if not google_event_id:
        return

    page = None
    mapped_internal_page_id = get_notion_page_id_by_google_id(google_event_id, "internal")
    if mapped_internal_page_id:
        page = notion_get_page(mapped_internal_page_id)
        if not page:
            remove_notion_page_id_by_google_id(google_event_id, "internal")
    if not page:
        page = notion_find_by_google_event_id(google_event_id, db_id=NOTION_EVENT_INTERNAL_DB_ID)
        if page:
            set_notion_page_id_by_google_id(google_event_id, page["id"], "internal")

    external_page = None
    if NOTION_EVENT_EXTERNAL_DB_ID:
        mapped_external_page_id = get_notion_page_id_by_google_id(google_event_id, "external")
        if mapped_external_page_id:
            external_page = notion_get_page(mapped_external_page_id)
            if not external_page:
                remove_notion_page_id_by_google_id(google_event_id, "external")
        if not external_page:
            external_page = notion_find_by_message_id(
                message_id=google_event_id,
                db_id=NOTION_EVENT_EXTERNAL_DB_ID,
            )
            if not external_page:
                # 旧データ移行向け: GoogleイベントID列での探索も試す
                external_page = notion_find_by_google_event_id(
                    google_event_id,
                    db_id=NOTION_EVENT_EXTERNAL_DB_ID,
                )
            if external_page:
                set_notion_page_id_by_google_id(google_event_id, external_page["id"], "external")

    if event.get("status") == "cancelled":
        # Discord削除IDの解決に使うため、archive前にページ参照を保持する。
        discord_hint_page = page
        discord_fallback_page = external_page
        if page:
            notion_archive_page(page)
            remove_notion_page_id_by_google_id(google_event_id, "internal")
        if external_page:
            notion_archive_page(external_page)
            remove_notion_page_id_by_google_id(google_event_id, "external")
        sync_to_discord(event, discord_hint_page, fallback_notion_page=discord_fallback_page)
        return

    name = event.get("summary") or "(no title)"
    content = event.get("description") or "(no content)"
    event_url = event.get("htmlLink")
    location = event.get("location")
    creator_id = event.get("creator", {}).get("email") or "unknown"
    _start_dt, end_dt = parse_google_event_times(event)
    now_utc = datetime.now(timezone.utc)
    skip_internal_create = (
        page is None
        and end_dt is not None
        and end_dt.astimezone(timezone.utc) <= now_utc
    )
    date_prop = build_notion_date(event)
    if not date_prop:
        return

    if page:
        updated = notion_update_event(
            page["id"],
            name=name,
            content=content,
            date_prop=date_prop,
            event_url=event_url,
            google_event_id=google_event_id,
            location=location,
        )
        if updated:
            logger.info("Notion updated: %s (%s)", name, google_event_id)
            set_notion_page_id_by_google_id(google_event_id, page["id"], "internal")
    elif not skip_internal_create:
        page_id = notion_create_event(
            name=name,
            content=content,
            date_prop=date_prop,
            creator_id=creator_id,
            event_url=event_url,
            google_event_id=google_event_id,
            location=location,
            db_id=NOTION_EVENT_INTERNAL_DB_ID,
            message_id="",
        )
        if not page_id:
            return
        logger.info("Notion created: %s (%s)", name, google_event_id)
        page = {"id": page_id, "properties": {}}
        set_notion_page_id_by_google_id(google_event_id, page_id, "internal")
    else:
        logger.info(
            "Skip internal Notion recreate for finished event: google_event_id=%s",
            google_event_id,
        )

    if NOTION_EVENT_EXTERNAL_DB_ID:
        if external_page:
            ext_ok = notion_update_event(
                external_page["id"],
                name=name,
                content=content,
                date_prop=date_prop,
                message_id=google_event_id,
            )
            if ext_ok:
                logger.info("Notion external updated: %s (%s)", name, google_event_id)
                set_notion_page_id_by_google_id(google_event_id, external_page["id"], "external")
        else:
            ext_page_id = notion_create_event(
                name=name,
                content=content,
                date_prop=date_prop,
                creator_id=creator_id,
                event_url=None,
                google_event_id=None,
                location=None,
                db_id=NOTION_EVENT_EXTERNAL_DB_ID,
                message_id=google_event_id,
            )
            if ext_page_id:
                logger.info("Notion external created: %s (%s)", name, google_event_id)
                external_page = {"id": ext_page_id, "properties": {}}
                set_notion_page_id_by_google_id(google_event_id, ext_page_id, "external")

    discord_event_id = sync_to_discord(event, page)
    if page and discord_event_id:
        notion_update_event(page["id"], message_id=discord_event_id)
    if external_page and discord_event_id:
        notion_update_event(external_page["id"], message_id=discord_event_id)


def sync_calendar():
    # ------------------------------------------------------------
    # Google Calendar 差分を取得して Notion / Discord 同期を実行する。
    #
    # 出力:
    # - True: 同期成功
    # - False: 同期失敗
    #
    # 処理概要:
    # 1) 必須env確認
    # 2) 前回カーソル読込
    # 3) 差分取得
    # 4) 各イベント反映
    # 5) 次回カーソル保存
    # ------------------------------------------------------------
    if not (NOTION_TOKEN and NOTION_EVENT_INTERNAL_DB_ID and GOOGLE_CALENDAR_ID):
        logger.error(
            "Missing required envs: NOTION_TOKEN/NOTION_EVENT_INTERNAL_ID/GOOGLE_CALENDAR_ID"
        )
        return False

    state = load_sync_state()
    updated_min = state.get("updated_min")
    logger.info("Sync start updated_min=%s", updated_min)

    events = list_updated_events(updated_min)
    logger.info("Google events fetched: %d", len(events))
    had_error = False
    for event in events:
        try:
            upsert_event(event)
        except Exception as exc:
            had_error = True
            logger.exception("Upsert failed event_id=%s err=%s", event.get("id"), exc)

    updated_values = [parse_rfc3339(e.get("updated")) for e in events]
    updated_values = [d for d in updated_values if d is not None]
    next_cursor = (
        max(updated_values).isoformat()
        if updated_values
        else datetime.now(timezone.utc).isoformat()
    )
    save_sync_state(next_cursor)
    logger.info(
        "Sync completed next_updated_min=%s events=%d had_error=%s",
        next_cursor,
        len(events),
        had_error,
    )
    return not had_error


def run_sync_guarded(source: str):
    # ------------------------------------------------------------
    # 同期処理の同時実行を抑止し、短時間の連打通知を間引く。
    #
    # 引数:
    # - source: 呼び出し元識別子（webhook/manual など）
    #
    # 出力:
    # - (True, "done"): 実際に同期実行し、成功
    # - (False, "done"): 実際に同期実行し、失敗
    # - (True, "cooldown"): 直近実行から短時間のためスキップ
    # - (True, "in_progress"): 別スレッドで実行中のためスキップ
    # ------------------------------------------------------------
    global _sync_last_run_epoch

    now = time.time()
    if now - _sync_last_run_epoch < max(0.0, SYNC_COOLDOWN_SECONDS):
        logger.info(
            "Sync skipped source=%s reason=cooldown cool=%.3fs",
            source,
            SYNC_COOLDOWN_SECONDS,
        )
        return True, "cooldown"

    locked = _sync_lock.acquire(blocking=False)
    if not locked:
        logger.info("Sync skipped source=%s reason=in_progress", source)
        return True, "in_progress"

    try:
        synced = sync_calendar()
        _sync_last_run_epoch = time.time()
        return synced, "done"
    finally:
        _sync_lock.release()


ensure_state_dir()
load_recent_message_ids()
load_gcal_discord_map()
load_gcal_notion_map()


@app.route("/gcal/webhook", methods=["POST"])
def gcal_webhook():
    # ------------------------------------------------------------
    # Google Calendar watch 通知受信エンドポイント。
    #
    # 出力:
    # - 204: 同期成功/重複通知スキップ
    # - 500: 同期失敗
    # ------------------------------------------------------------
    goog_channel = request.headers.get("X-Goog-Channel-ID")
    goog_message_num = request.headers.get("X-Goog-Message-Number")
    goog_state = request.headers.get("X-Goog-Resource-State")
    if goog_channel and goog_message_num:
        dedupe_key = f"goog:{goog_channel}:{goog_message_num}"
        if register_message_id(dedupe_key):
            logger.info("Duplicate Google webhook message skipped key=%s", dedupe_key)
            return "", 204

    logger.info(
        "Webhook received mode=direct ch=%s state=%s msg=%s",
        goog_channel,
        goog_state,
        goog_message_num,
    )
    synced, reason = run_sync_guarded("webhook")
    if reason != "done":
        return "", 204
    if not synced:
        return "sync failed", 500
    return "", 204


@app.route("/gcal/sync", methods=["GET", "POST"])
def manual_sync():
    # ------------------------------------------------------------
    # 手動同期エンドポイント。
    #
    # 出力:
    # - 200: 同期成功
    # - 500: 同期失敗
    # ------------------------------------------------------------
    synced, _ = run_sync_guarded("manual")
    return ("ok", 200) if synced else ("sync failed", 500)


@app.route("/health", methods=["GET"])
def health():
    # ヘルスチェック用エンドポイント。
    return "ok", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


