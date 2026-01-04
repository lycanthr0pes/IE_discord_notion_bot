import json
import os
import uuid
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
GCAL_PUBSUB_TOPIC = os.getenv("GCAL_PUBSUB_TOPIC")
GCAL_WEBHOOK_URL = os.getenv("GCAL_WEBHOOK_URL")
WATCH_CHANNEL_ID = os.getenv("WATCH_CHANNEL_ID")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
STATE_FILE = "gcal_watch_state.json"


def load_service_account_info():
    # JSON文字列またはJSONファイルからサービスアカウント情報を取得
    json_env = GOOGLE_SERVICE_ACCOUNT_JSON
    if json_env:
        if os.path.exists(json_env):
            with open(json_env, "r", encoding="utf-8") as f:
                return json.load(f)
        try:
            return json.loads(json_env)
        except json.JSONDecodeError:
            return None

    if GOOGLE_SERVICE_ACCOUNT_JSON_PATH and os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON_PATH):
        with open(GOOGLE_SERVICE_ACCOUNT_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def get_calendar_service():
    # Calendar APIクライアントを構築
    info = load_service_account_info()
    if not info:
        return None
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def build_watch_request(channel_id):
    # Pub/Subトピックがあればそれを使い、無ければWebhook直叩きで登録
    if GCAL_PUBSUB_TOPIC:
        return {
            "id": channel_id,
            "type": "web_hook",
            "address": "https://pubsub.googleapis.com/google.calendar.v3.channels",
            "params": {"topicName": GCAL_PUBSUB_TOPIC},
        }
    if not GCAL_WEBHOOK_URL:
        return None
    return {"id": channel_id, "type": "web_hook", "address": GCAL_WEBHOOK_URL}


def save_state(payload):
    # renew.py 用にチャンネル情報を保存
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    # 初回のwatch登録（手動で1回実行）
    if not GOOGLE_CALENDAR_ID:
        raise SystemExit("GOOGLE_CALENDAR_ID is required")

    service = get_calendar_service()
    if not service:
        raise SystemExit("Service account info not found or invalid")

    # チャンネルIDは任意の一意文字列。指定が無ければUUIDを使う
    channel_id = WATCH_CHANNEL_ID or f"gcal-{uuid.uuid4()}"
    body = build_watch_request(channel_id)
    if not body:
        raise SystemExit("GCAL_PUBSUB_TOPIC or GCAL_WEBHOOK_URL is required")

    response = service.events().watch(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()
    save_state(
        {
            "channel_id": response.get("id"),
            "resource_id": response.get("resourceId"),
            "expiration": response.get("expiration"),
            "calendar_id": GOOGLE_CALENDAR_ID,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    print("watch registered:", response)


if __name__ == "__main__":
    main()
