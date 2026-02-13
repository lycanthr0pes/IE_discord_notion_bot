import json
import logging
import os
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
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
    # 迺ｰ蠅・､画焚繧貞叙蠕励＠縲∵枚蟄怜・縺ｮ蝣ｴ蜷医・蜑榊ｾ檎ｩｺ逋ｽ繧帝勁蜴ｻ縺励※霑斐☆縲・    #
    # 蠑墓焚:
    # - name: 迺ｰ蠅・､画焚蜷・    # - default: 譛ｪ險ｭ螳壽凾縺ｮ繝・ヵ繧ｩ繝ｫ繝亥､
    #
    # 蜃ｺ蜉・
    # - 譁・ｭ怜・: strip蠕後∫ｩｺ譁・ｭ励↑繧・default
    # - 髱樊枚蟄怜・: 縺昴・縺ｾ縺ｾ
    # ------------------------------------------------------------
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
        return value if value else default
    return value


NOTION_TOKEN = getenv_clean("NOTION_TOKEN")
NOTION_EVENT_INTERNAL_DB_ID = getenv_clean("NOTION_EVENT_INTERNAL_ID")
NOTION_LOCATION_PROPERTY = getenv_clean("NOTION_LOCATION_PROPERTY", "場所")

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
DEDUPE_MAX_IDS = int(getenv_clean("DEDUPE_MAX_IDS", "1000"))

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

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_calendar_service = None
_processed_message_ids = deque(maxlen=max(100, DEDUPE_MAX_IDS))
_processed_message_set = set()
_gcal_discord_map = {}


def parse_rfc3339(value):
    # ------------------------------------------------------------
    # RFC3339 蠖｢蠑上・譌･譎よ枚蟄怜・繧・datetime 縺ｫ螟画鋤縺吶ｋ縲・    #
    # 蠑墓焚:
    # - value: 螟画鋤蟇ｾ雎｡譁・ｭ怜・
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: datetime
    # - 螟ｱ謨・ None
    # ------------------------------------------------------------
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def ensure_state_dir():
    # ------------------------------------------------------------
    # 迥ｶ諷九ヵ繧｡繧､繝ｫ菫晏ｭ伜・繝・ぅ繝ｬ繧ｯ繝医Μ繧剃ｽ懈・縺吶ｋ縲・    #
    # 蜃ｺ蜉・
    # - 縺ｪ縺暦ｼ亥､ｱ謨玲凾縺ｯ繝ｭ繧ｰ蜃ｺ蜉幢ｼ・    # ------------------------------------------------------------
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
    except Exception as exc:
        logger.error("Failed to create STATE_DIR=%s: %s", STATE_DIR, exc)


def load_recent_message_ids():
    # ------------------------------------------------------------
    # 逶ｴ霑大・逅・ｸ医∩繝｡繝・そ繝ｼ繧ｸID繧堤憾諷九ヵ繧｡繧､繝ｫ縺九ｉ蠕ｩ蜈・☆繧九・    #
    # 蜃ｺ蜉・
    # - 縺ｪ縺暦ｼ医Γ繝｢繝ｪ荳翫・dedupe讒矩縺ｫ蜿肴丐・・    # ------------------------------------------------------------
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
    # 逶ｴ霑大・逅・ｸ医∩繝｡繝・そ繝ｼ繧ｸID繧堤憾諷九ヵ繧｡繧､繝ｫ縺ｸ菫晏ｭ倥☆繧九・    #
    # 蜃ｺ蜉・
    # - 縺ｪ縺暦ｼ亥､ｱ謨玲凾縺ｯ繝ｭ繧ｰ蜃ｺ蜉幢ｼ・    # ------------------------------------------------------------
    ensure_state_dir()
    try:
        with open(DEDUPE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ids": list(_processed_message_ids)}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save dedupe state: %s", exc)


def register_message_id(message_id):
    # ------------------------------------------------------------
    # 繝｡繝・そ繝ｼ繧ｸID繧帝㍾隍・賜髯､繧ｻ繝・ヨ縺ｸ逋ｻ骭ｲ縺吶ｋ縲・    #
    # 蠑墓焚:
    # - message_id: 蛻､螳壼ｯｾ雎｡ID
    #
    # 蜃ｺ蜉・
    # - True: 譌｢縺ｫ逋ｻ骭ｲ貂医∩・磯㍾隍・ｼ・    # - False: 譁ｰ隕冗匳骭ｲ
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
    # Google繧､繝吶Φ繝・D -> Discord繧､繝吶Φ繝・D 縺ｮ蟇ｾ蠢懆｡ｨ繧貞ｾｩ蜈・☆繧九・    #
    # 蜃ｺ蜉・
    # - 縺ｪ縺暦ｼ医Γ繝｢繝ｪ荳翫・ _gcal_discord_map 縺ｫ蜿肴丐・・    # ------------------------------------------------------------
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
    # Google->Discord 蟇ｾ蠢懆｡ｨ繧堤憾諷九ヵ繧｡繧､繝ｫ縺ｸ菫晏ｭ倥☆繧九・    #
    # 蜃ｺ蜉・
    # - 縺ｪ縺暦ｼ亥､ｱ謨玲凾縺ｯ繝ｭ繧ｰ蜃ｺ蜉幢ｼ・    # ------------------------------------------------------------
    ensure_state_dir()
    try:
        with open(GCAL_DISCORD_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump({"map": _gcal_discord_map}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Failed to save gcal_discord_map: %s", exc)


def get_discord_event_id_by_google_id(google_event_id):
    # GoogleイベントIDからDiscordイベントIDを取得する。
    if not google_event_id:
        return None
    return _gcal_discord_map.get(str(google_event_id))


def set_discord_event_id_by_google_id(google_event_id, discord_event_id):
    # GoogleイベントIDとDiscordイベントIDの対応を保存する。
    if not google_event_id or not discord_event_id:
        return
    _gcal_discord_map[str(google_event_id)] = str(discord_event_id)
    save_gcal_discord_map()


def remove_discord_event_id_by_google_id(google_event_id):
    # GoogleイベントIDに対応するDiscordイベントIDを削除する。
    if not google_event_id:
        return
    _gcal_discord_map.pop(str(google_event_id), None)
    save_gcal_discord_map()


def load_service_account_info():
    # ------------------------------------------------------------
    # Google Service Account 諠・ｱ繧堤腸蠅・､画焚/繝輔ぃ繧､繝ｫ縺九ｉ隱ｭ縺ｿ霎ｼ繧縲・    #
    # 蜈･蜉・
    # - GOOGLE_SERVICE_ACCOUNT_JSON:
    #   1) JSON譁・ｭ怜・
    #   2) JSON繝輔ぃ繧､繝ｫ繝代せ
    # - GOOGLE_SERVICE_ACCOUNT_JSON_PATH:
    #   譏守､ｺ逧・↑JSON繝輔ぃ繧､繝ｫ繝代せ
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: service_account_info(dict)
    # - 螟ｱ謨・ None
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
    # Google Calendar API service 繧貞・譛溷喧縺励∝・蛻ｩ逕ｨ縺吶ｋ縲・    #
    # 蜃ｺ蜉・
    # - 謌仙粥: googleapiclient service
    # - 螟ｱ謨・ None
    #
    # 蛯呵・
    # - _calendar_service 縺ｫ繧ｭ繝｣繝・す繝･縺怜・蛻ｩ逕ｨ縺吶ｋ
    # - GOOGLE_CALENDAR_ID 譛ｪ險ｭ螳壽凾縺ｯ螟ｱ謨・    # ------------------------------------------------------------
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
    # 蜷梧悄繧ｫ繝ｼ繧ｽ繝ｫ迥ｶ諷・updated_min)繧定ｪｭ縺ｿ霎ｼ繧縲・    #
    # 蜃ｺ蜉・
    # - 謌仙粥: 迥ｶ諷掬ict
    # - 螟ｱ謨・譛ｪ菴懈・: {}
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
    # 蜷梧悄繧ｫ繝ｼ繧ｽ繝ｫ(updated_min)繧堤憾諷九ヵ繧｡繧､繝ｫ縺ｫ菫晏ｭ倥☆繧九・    #
    # 蠑墓焚:
    # - updated_min: 谺｡蝗槫酔譛溘↓菴ｿ縺・き繝ｼ繧ｽ繝ｫ
    # ------------------------------------------------------------
    ensure_state_dir()
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_min": updated_min}, f, ensure_ascii=False, indent=2)


def list_updated_events(updated_min):
    # ------------------------------------------------------------
    # Google Calendar 縺ｮ譖ｴ譁ｰ繧､繝吶Φ繝井ｸ隕ｧ繧貞叙蠕励☆繧九・    #
    # 蠑墓焚:
    # - updated_min: 蜑榊屓蜷梧悄繧ｫ繝ｼ繧ｽ繝ｫ・・SO譁・ｭ怜・・・    #
    # 蜃ｺ蜉・
    # - 謌仙粥: 繧､繝吶Φ繝磯・蛻・list[dict])
    # - 螟ｱ謨・ 遨ｺ驟榊・
    #
    # 謖吝虚:
    # - 蛻晏屓縺ｯ30譌･lookback縺ｧ蜿門ｾ・    # - 蟾ｮ蛻・叙繧翫％縺ｼ縺怜屓驕ｿ縺ｮ縺溘ａ2蛻・ｷｻ縺肴綾縺・    # - updatedMinTooLongAgo(410)譎ゅ・ updatedMin 縺ｪ縺励〒蜈ｨ蜿門ｾ励∈繝輔か繝ｼ繝ｫ繝舌ャ繧ｯ
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
    # Google繧､繝吶Φ繝医°繧・Notion date 繝励Ο繝代ユ繧｣蠖｢蠑上ｒ菴懊ｋ縲・    #
    # 蠑墓焚:
    # - event: Google Calendar event(dict)
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: {"start": ..., "end": ...}
    # - 螟ｱ謨・ None
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
    # Notion繝壹・繧ｸ縺ｮ rich_text 繝励Ο繝代ユ繧｣蜈磯ｭ繝・く繧ｹ繝医ｒ謚ｽ蜃ｺ縺吶ｋ縲・    #
    # 蠑墓焚:
    # - page: Notion繝壹・繧ｸ(dict)
    # - prop_name: 繝励Ο繝代ユ繧｣蜷・    #
    # 蜃ｺ蜉・
    # - 謌仙粥: 譁・ｭ怜・
    # - 螟ｱ謨・遨ｺ: None
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


def notion_find_by_google_event_id(google_event_id):
    # ------------------------------------------------------------
    # Google繧､繝吶Φ繝・D繧偵く繝ｼ縺ｫ Notion 蜀・ΚDB縺九ｉ繝壹・繧ｸ繧・莉ｶ讀懃ｴ｢縺吶ｋ縲・    #
    # 蠑墓焚:
    # - google_event_id: Google Calendar event.id
    #
    # 蜃ｺ蜉・
    # - 隕九▽縺九▲縺溷ｴ蜷・ Notion繝壹・繧ｸ(dict)
    # - 隕九▽縺九ｉ縺ｪ縺・ｴ蜷・螟ｱ謨・ None
    # ------------------------------------------------------------
    if not NOTION_EVENT_INTERNAL_DB_ID:
        logger.error("NOTION_EVENT_INTERNAL_ID is not set")
        return None

    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_INTERNAL_DB_ID}/query"
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
    # Notion繝壹・繧ｸ繧帝Κ蛻・峩譁ｰ縺吶ｋ縲・    #
    # 蠑墓焚:
    # - page_id: 譖ｴ譁ｰ蟇ｾ雎｡繝壹・繧ｸID
    # - name/content/date_prop/event_url/google_event_id/page_uuid/message_id/location:
    #   None 莉･螟悶・鬆・岼縺縺第峩譁ｰ蟇ｾ雎｡縺ｫ蜷ｫ繧√ｋ
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: True
    # - 螟ｱ謨・ False
    #
    # 蛯呵・
    # - location 縺ｯ place 蝙九・繝ｭ繝代ユ繧｣縺ｨ縺励※譖ｴ譁ｰ縺吶ｋ
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
    if location is not None and NOTION_LOCATION_PROPERTY:
        props[NOTION_LOCATION_PROPERTY] = {
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


def notion_create_event(name, content, date_prop, creator_id, event_url, google_event_id, location=None):
    # ------------------------------------------------------------
    # Notion 蜀・ΚDB縺ｫ繧､繝吶Φ繝医・繝ｼ繧ｸ繧呈眠隕丈ｽ懈・縺吶ｋ縲・    #
    # 蠑墓焚:
    # - name/content/date_prop/creator_id/event_url/google_event_id/location
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: 菴懈・縺励◆繝壹・繧ｸID(str)
    # - 螟ｱ謨・ None
    #
    # 蛯呵・
    # - 菴懈・蠕後↓繝壹・繧ｸ閾ｪ霄ｫ縺ｮID繧偵・繝ｼ繧ｸID繝励Ο繝代ユ繧｣縺ｸ蜿肴丐縺吶ｋ
    # ------------------------------------------------------------
    url = "https://api.notion.com/v1/pages"
    data = {
        "parent": {"database_id": NOTION_EVENT_INTERNAL_DB_ID},
        "properties": {
            NOTION_PROP_TITLE: {"title": [{"text": {"content": name}}]},
            NOTION_PROP_CONTENT: {"rich_text": [{"text": {"content": content}}]},
            NOTION_PROP_DATE: {"date": date_prop},
            NOTION_PROP_MESSAGE_ID: {"rich_text": [{"text": {"content": ""}}]},
            NOTION_PROP_CREATOR_ID: {"rich_text": [{"text": {"content": str(creator_id)}}]},
            NOTION_PROP_PAGE_ID: {"rich_text": [{"text": {"content": ""}}]},
            NOTION_PROP_EVENT_URL: {"url": event_url},
            NOTION_PROP_GOOGLE_EVENT_ID: {
                "rich_text": [{"text": {"content": str(google_event_id)}}]
            },
        },
    }
    if location is not None and NOTION_LOCATION_PROPERTY:
        data["properties"][NOTION_LOCATION_PROPERTY] = {
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
    # Notion繝壹・繧ｸ繧偵い繝ｼ繧ｫ繧､繝悶☆繧九・    #
    # 蠑墓焚:
    # - page: Notion繝壹・繧ｸ(dict)
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: True
    # - 螟ｱ謨・ False
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
    # Discord 蜷梧悄縺悟ｮ溯｡悟庄閭ｽ縺九ｒ蛻､螳壹☆繧九・    #
    # 蜃ｺ蜉・
    # - True: 蜷梧悄蜿ｯ閭ｽ
    # - False: 蜷梧悄荳榊庄・育┌蜉ｹ蛹・蠢・・nv荳崎ｶｳ・・    # ------------------------------------------------------------
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
    # Discord REST API 繧貞ｮ溯｡後☆繧句・騾夐未謨ｰ縲・    #
    # 蠑墓焚:
    # - method: HTTP繝｡繧ｽ繝・ラ
    # - path: /api/v10 莉･髯阪・繝代せ
    # - payload: 繝ｪ繧ｯ繧ｨ繧ｹ繝・SON・井ｻｻ諢擾ｼ・    #
    # 蜃ｺ蜉・
    # - 謌仙粥(204): {}
    # - 謌仙粥(JSON): dict
    # - 螟ｱ謨・ None
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
    # Google繧､繝吶Φ繝医・ start/end 繧・Discord逕ｨ datetime 縺ｫ豁｣隕丞喧縺吶ｋ縲・    #
    # 蠑墓焚:
    # - event: Google Calendar event(dict)
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: (start_dt, end_dt)
    # - 螟ｱ謨・ (None, None)
    #
    # 蛯呵・
    # - 邨ゆｺ・凾蛻ｻ譛ｪ謖・ｮ壽凾縺ｯ +1譎る俣
    # - 邨ゆｺ・<= 髢句ｧ・縺ｮ蝣ｴ蜷医ｂ +1譎る俣縺ｧ陬懈ｭ｣
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
    # Discord説明文を生成し、origin marker を付与する。
    marker = f"{DISCORD_ORIGIN_MARKER_PREFIX}{google_event_id}]"
    base = (description or "").strip()
    text = f"{base}\n\n{marker}" if base else marker
    return text[:DISCORD_DESCRIPTION_LIMIT]


def build_discord_payload(event):
    # ------------------------------------------------------------
    # Google繧､繝吶Φ繝医°繧・Discord Scheduled Event 逕ｨpayload繧堤ｵ・∩遶九※繧九・    #
    # 蠑墓焚:
    # - event: Google Calendar event(dict)
    #
    # 蜃ｺ蜉・
    # - 謌仙粥: payload(dict)
    # - 螟ｱ謨・ None
    #
    # 蛯呵・
    # - 隱ｬ譏取枚譛ｫ蟆ｾ縺ｫ origin marker([gcal-id:...]) 繧剃ｻ倅ｸ弱☆繧・    # ------------------------------------------------------------
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


def sync_to_discord(event, notion_page):
    # ------------------------------------------------------------
    # Google繧､繝吶Φ繝医ｒ Discord Scheduled Event 縺ｫ蜷梧悄縺吶ｋ縲・    #
    # 蠑墓焚:
    # - event: Google Calendar event(dict)
    # - notion_page: 蟇ｾ蠢懊☆繧起otion繝壹・繧ｸ(dict|None)
    #
    # 蜃ｺ蜉・
    # - 菴懈・/譖ｴ譁ｰ謌仙粥譎・ Discord繧､繝吶Φ繝・D(str)
    # - 蜑企勁譎・譛ｪ蜷梧悄譎・螟ｱ謨玲凾: None
    #
    # 謖吝虚:
    # - cancel 繧､繝吶Φ繝医・ Discord 蛛ｴ繧貞炎髯､
    # - Notion縺ｮ繝｡繝・そ繝ｼ繧ｸID -> 豌ｸ邯嗄ap 縺ｮ鬆・〒蟇ｾ蠢廬D繧定ｧ｣豎ｺ
    # - ID縺檎┌縺代ｌ縺ｰ譁ｰ隕丈ｽ懈・
    # ------------------------------------------------------------
    if not discord_sync_available():
        return None
    google_event_id = event.get("id")
    if not google_event_id:
        return None

    notion_discord_id = notion_extract_rich_text(notion_page, NOTION_PROP_MESSAGE_ID)
    mapped_discord_id = get_discord_event_id_by_google_id(google_event_id)
    discord_event_id = notion_discord_id or mapped_discord_id

    if event.get("status") == "cancelled":
        if discord_event_id:
            if discord_delete_event(discord_event_id):
                logger.info(
                    "Discord event deleted by Google cancel: google_event_id=%s discord_event_id=%s",
                    google_event_id,
                    discord_event_id,
                )
        remove_discord_event_id_by_google_id(google_event_id)
        return None

    if discord_event_id:
        updated = discord_update_event(discord_event_id, event)
        if updated is not None:
            resolved_id = str(updated.get("id") or discord_event_id)
            set_discord_event_id_by_google_id(google_event_id, resolved_id)
            return resolved_id

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
    # 1莉ｶ縺ｮ Google繧､繝吶Φ繝医ｒ Notion/Discord 縺ｫ蜿肴丐縺吶ｋ縲・    #
    # 蠑墓焚:
    # - event: Google Calendar event(dict)
    #
    # 蜃ｺ蜉・
    # - 縺ｪ縺・    #
    # 謖吝虚:
    # - cancelled: Notion繧｢繝ｼ繧ｫ繧､繝・+ Discord蜑企勁
    # - active: Notion upsert + Discord create/update
    # - Discord繧､繝吶Φ繝・D縺ｯ Notion 縺ｮ繝｡繝・そ繝ｼ繧ｸID縺ｫ繧ゆｿ晏ｭ倥☆繧・    # ------------------------------------------------------------
    google_event_id = event.get("id")
    if not google_event_id:
        return

    page = notion_find_by_google_event_id(google_event_id)

    if event.get("status") == "cancelled":
        if page:
            notion_archive_page(page)
        sync_to_discord(event, page)
        return

    name = event.get("summary") or "(繧ｿ繧､繝医Ν縺ｪ縺・"
    content = event.get("description") or "(蜀・ｮｹ縺ｪ縺・"
    event_url = event.get("htmlLink")
    location = event.get("location")
    creator_id = event.get("creator", {}).get("email") or "unknown"
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
    else:
        page_id = notion_create_event(
            name=name,
            content=content,
            date_prop=date_prop,
            creator_id=creator_id,
            event_url=event_url,
            google_event_id=google_event_id,
            location=location,
        )
        if not page_id:
            return
        logger.info("Notion created: %s (%s)", name, google_event_id)
        page = {"id": page_id, "properties": {}}

    discord_event_id = sync_to_discord(event, page)
    if page and discord_event_id:
        notion_update_event(page["id"], message_id=discord_event_id)


def sync_calendar():
    # ------------------------------------------------------------
    # Google Calendar 蟾ｮ蛻・ｒ蜿門ｾ励＠縲¨otion/Discord 蜷梧悄繧剃ｸ諡ｬ螳溯｡後☆繧九・    #
    # 蜃ｺ蜉・
    # - True: 蜷梧悄謌仙粥・磯㍾螟ｧ繧ｨ繝ｩ繝ｼ縺ｪ縺暦ｼ・    # - False: 蜷梧悄螟ｱ謨・    #
    # 蜃ｦ逅・ｦりｦ・
    # 1) 蠢・・nv遒ｺ隱・    # 2) 繧ｫ繝ｼ繧ｽ繝ｫ(updated_min)繧定ｪｭ縺ｿ霎ｼ縺ｿ
    # 3) 蟾ｮ蛻・う繝吶Φ繝亥叙蠕・    # 4) 蜷・う繝吶Φ繝医ｒ upsert_event 縺ｧ蜿肴丐
    # 5) 谺｡蝗槭き繝ｼ繧ｽ繝ｫ繧剃ｿ晏ｭ・    # ------------------------------------------------------------
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


ensure_state_dir()
load_recent_message_ids()
load_gcal_discord_map()


@app.route("/gcal/webhook", methods=["POST"])
def gcal_webhook():
    # ------------------------------------------------------------
    # Google Calendar watch 騾夂衍縺ｮ蜿嶺ｿ｡繧ｨ繝ｳ繝峨・繧､繝ｳ繝医・    #
    # 蜃ｺ蜉・
    # - 204: 蜷梧悄謌仙粥 or 驥崎､・夂衍繧偵せ繧ｭ繝・・
    # - 500: 蜷梧悄螟ｱ謨・    #
    # 蛯呵・
    # - X-Goog-Channel-ID / X-Goog-Message-Number 縺ｧ驥崎､・賜髯､繧定｡後≧
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
    synced = sync_calendar()
    if not synced:
        return "sync failed", 500
    return "", 204


@app.route("/gcal/sync", methods=["GET", "POST"])
def manual_sync():
    # ------------------------------------------------------------
    # 謇句虚蜷梧悄繧ｨ繝ｳ繝峨・繧､繝ｳ繝医・    #
    # 蜃ｺ蜉・
    # - 200: 蜷梧悄謌仙粥
    # - 500: 蜷梧悄螟ｱ謨・    # ------------------------------------------------------------
    synced = sync_calendar()
    return ("ok", 200) if synced else ("sync failed", 500)


@app.route("/health", methods=["GET"])
def health():
    # ヘルスチェック用エンドポイント。
    return "ok", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


