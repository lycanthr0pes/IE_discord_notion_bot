import json
import os
import logging
from datetime import datetime, timezone, timedelta

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
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
        return value if value else default
    return value


NOTION_TOKEN = getenv_clean("NOTION_TOKEN")
NOTION_EVENT_INTERNAL_DB_ID = getenv_clean("NOTION_EVENT_INTERNAL_ID")

GOOGLE_CALENDAR_ID = getenv_clean("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SYNC_STATE_FILE = "gcal_sync_state.json"

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_calendar_service = None


def parse_rfc3339(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def load_service_account_info():
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
    if not os.path.exists(SYNC_STATE_FILE):
        return {}
    try:
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sync_state(updated_min):
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_min": updated_min}, f, ensure_ascii=False, indent=2)


def list_updated_events(updated_min):
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


def notion_find_by_google_event_id(google_event_id):
    if not NOTION_EVENT_INTERNAL_DB_ID:
        logger.error("NOTION_EVENT_INTERNAL_ID is not set")
        return None

    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_INTERNAL_DB_ID}/query"
    data = {
        "filter": {
            "property": "GoogleイベントID",
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
):
    props = {}
    if name is not None:
        props["イベント名"] = {"title": [{"text": {"content": name}}]}
    if content is not None:
        props["内容"] = {"rich_text": [{"text": {"content": content}}]}
    if date_prop is not None:
        props["日時"] = {"date": date_prop}
    if event_url is not None:
        props["イベントURL"] = {"url": event_url}
    if google_event_id is not None:
        props["GoogleイベントID"] = {"rich_text": [{"text": {"content": str(google_event_id)}}]}
    if page_uuid is not None:
        props["ページID"] = {"rich_text": [{"text": {"content": str(page_uuid)}}]}

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


def notion_create_event(name, content, date_prop, creator_id, event_url, google_event_id):
    url = "https://api.notion.com/v1/pages"
    data = {
        "parent": {"database_id": NOTION_EVENT_INTERNAL_DB_ID},
        "properties": {
            "イベント名": {"title": [{"text": {"content": name}}]},
            "内容": {"rich_text": [{"text": {"content": content}}]},
            "日時": {"date": date_prop},
            "メッセージID": {"rich_text": [{"text": {"content": ""}}]},
            "作成者ID": {"rich_text": [{"text": {"content": str(creator_id)}}]},
            "ページID": {"rich_text": [{"text": {"content": ""}}]},
            "イベントURL": {"url": event_url},
            "GoogleイベントID": {"rich_text": [{"text": {"content": str(google_event_id)}}]},
        },
    }
    res = requests.post(url, headers=headers, json=data, timeout=30)
    if res.status_code not in (200, 201):
        logger.error("Notion create error: %s", res.text)
        return None

    page_id = res.json()["id"]
    notion_update_event(page_id, page_uuid=page_id)
    return page_id


def notion_archive_by_google_event_id(google_event_id):
    page = notion_find_by_google_event_id(google_event_id)
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


def upsert_event_to_notion(event):
    google_event_id = event.get("id")
    if not google_event_id:
        return

    if event.get("status") == "cancelled":
        notion_archive_by_google_event_id(google_event_id)
        return

    name = event.get("summary") or "(名称なし)"
    content = event.get("description") or "(内容なし)"
    event_url = event.get("htmlLink")
    creator_id = event.get("creator", {}).get("email") or "unknown"
    date_prop = build_notion_date(event)
    if not date_prop:
        return

    page = notion_find_by_google_event_id(google_event_id)
    if page:
        ok = notion_update_event(
            page["id"],
            name=name,
            content=content,
            date_prop=date_prop,
            event_url=event_url,
            google_event_id=google_event_id,
        )
        if ok:
            logger.info("Notion updated: %s (%s)", name, google_event_id)
        return

    page_id = notion_create_event(
        name=name,
        content=content,
        date_prop=date_prop,
        creator_id=creator_id,
        event_url=event_url,
        google_event_id=google_event_id,
    )
    if page_id:
        logger.info("Notion created: %s (%s)", name, google_event_id)


def sync_calendar_to_notion():
    if not (NOTION_TOKEN and NOTION_EVENT_INTERNAL_DB_ID and GOOGLE_CALENDAR_ID):
        logger.error(
            "Missing required envs: NOTION_TOKEN/NOTION_EVENT_INTERNAL_ID/GOOGLE_CALENDAR_ID"
        )
        return

    state = load_sync_state()
    updated_min = state.get("updated_min")
    logger.info("Sync start updated_min=%s", updated_min)

    events = list_updated_events(updated_min)
    logger.info("Google events fetched: %d", len(events))
    for event in events:
        upsert_event_to_notion(event)

    updated_values = [parse_rfc3339(e.get("updated")) for e in events]
    updated_values = [d for d in updated_values if d is not None]
    next_cursor = (
        max(updated_values).isoformat()
        if updated_values
        else datetime.now(timezone.utc).isoformat()
    )
    save_sync_state(next_cursor)
    logger.info("Sync completed next_updated_min=%s", next_cursor)


@app.route("/gcal/webhook", methods=["POST"])
def gcal_webhook():
    logger.info(
        "Webhook received ch=%s state=%s msg=%s",
        request.headers.get("X-Goog-Channel-ID"),
        request.headers.get("X-Goog-Resource-State"),
        request.headers.get("X-Goog-Message-Number"),
    )
    sync_calendar_to_notion()
    return "", 204


@app.route("/gcal/sync", methods=["GET", "POST"])
def manual_sync():
    sync_calendar_to_notion()
    return "ok", 200


@app.route("/health", methods=["GET"])
def health():
    return "ok", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
