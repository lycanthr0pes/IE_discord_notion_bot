import json
import os
import uuid
import logging
from datetime import datetime, timezone

from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("watcher.register")

def getenv_clean(name: str, default=None):
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
        return value if value else default
    return value

GOOGLE_CALENDAR_ID = getenv_clean("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
GCAL_PUBSUB_TOPIC = getenv_clean("GCAL_PUBSUB_TOPIC")
GCAL_WEBHOOK_URL = getenv_clean("GCAL_WEBHOOK_URL")
WATCH_CHANNEL_ID = getenv_clean("WATCH_CHANNEL_ID")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
STATE_FILE = "gcal_watch_state.json"

print("register.py started", flush=True)

def load_service_account_info():
    # ------------------------------------------------------------
    # Google Service Account の認証情報(JSON/dict)を読み込んで返す。
    #
    # 引数:
    # - なし（環境変数を参照）
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
    # ------------------------------------------------------------
    # Google Calendar API クライアントを生成して返す。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - 成功: Calendar API service オブジェクト
    # - 失敗: None
    #
    # 備考:
    # 認証情報を解決できない場合は None を返し、呼び出し元で終了判定する。
    # ------------------------------------------------------------
    # Calendar APIクライアントを構築
    info = load_service_account_info()
    if not info:
        return None
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def build_watch_request(channel_id):
    # ------------------------------------------------------------
    # Calendar watch 登録用のリクエストボディを組み立てる。
    #
    # 引数:
    # - channel_id: watch チャンネルID（一意文字列）
    #
    # 出力:
    # - 成功: watch request dict
    # - 失敗: None
    #
    # 備考:
    # GCAL_PUBSUB_TOPIC がある場合は Pub/Sub 経由で通知し、
    # 無い場合は GCAL_WEBHOOK_URL へ直接通知する。
    # ------------------------------------------------------------
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
    # ------------------------------------------------------------
    # watch チャンネル情報をローカル状態ファイルへ保存する。
    #
    # 引数:
    # - payload: channel_id / resource_id / expiration などの情報
    #
    # 出力:
    # - なし
    #
    # 備考:
    # renew.py が次回更新時に旧チャンネル停止と再登録に利用する。
    # ------------------------------------------------------------
    # renew.py 用にチャンネル情報を保存
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    # ------------------------------------------------------------
    # Google Calendar watch の初回登録を実行するエントリポイント。
    #
    # 処理概要:
    # 1) 必須設定（カレンダーID/認証情報）を検証
    # 2) watch request を生成
    # 3) events.watch() を実行
    # 4) 返却されたチャンネル情報を state に保存
    #
    # 出力:
    # - なし（watch 登録・状態保存）
    # ------------------------------------------------------------
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

    # Google に watch 登録(resource_id は watch 内の監視対象リソースの識別子)
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
    logger.info("watch registered: %s", response)


if __name__ == "__main__":
    main()

print("register.py complate", flush=True)
