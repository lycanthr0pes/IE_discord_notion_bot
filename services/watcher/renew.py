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
logger = logging.getLogger("watcher.renew")


def getenv_clean(name: str, default=None):
    # ------------------------------------------------------------
    # 環境変数を取得し、文字列の場合は前後空白を除去して返す。
    #
    # 引数:
    # - name: 環境変数名
    # - default: 未設定時のデフォルト値
    #
    # 出力:
    # - 文字列: strip後、空文字なら default
    # - 非文字列: そのまま
    # ------------------------------------------------------------
    value = os.getenv(name, default)
    if isinstance(value, str):
        value = value.strip()
        return value if value else default
    return value


GOOGLE_CALENDAR_ID = getenv_clean("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = getenv_clean("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")
GCAL_WEBHOOK_URL = getenv_clean("GCAL_WEBHOOK_URL")
WATCH_CHANNEL_ID = getenv_clean("WATCH_CHANNEL_ID")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
STATE_FILE = "gcal_watch_state.json"


def load_service_account_info():
    # ------------------------------------------------------------
    # Google Service Account 情報を環境変数/ファイルから読み込む。
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
    # Google Calendar API service を初期化して返す。
    #
    # 出力:
    # - 成功: googleapiclient service
    # - 失敗: None
    # ------------------------------------------------------------
    info = load_service_account_info()
    if not info:
        return None
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def build_watch_request(channel_id):
    # ------------------------------------------------------------
    # watch 更新用リクエストを組み立てる。
    #
    # 引数:
    # - channel_id: watch チャンネルID
    #
    # 出力:
    # - 成功: watch request body(dict)
    # - 失敗: None
    #
    # 備考:
    # webhook-only 構成のため、通知先は GCAL_WEBHOOK_URL を使う。
    # ------------------------------------------------------------
    if not GCAL_WEBHOOK_URL:
        return None
    logger.info("watch delivery mode=direct_webhook url=%s", GCAL_WEBHOOK_URL)
    return {"id": channel_id, "type": "web_hook", "address": GCAL_WEBHOOK_URL}


def load_state():
    # ------------------------------------------------------------
    # 既存 watch 状態を状態ファイルから読み込む。
    #
    # 出力:
    # - 成功: 状態dict
    # - 失敗/未作成: {}
    # ------------------------------------------------------------
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(payload):
    # ------------------------------------------------------------
    # watch 更新結果を状態ファイルへ保存する。
    #
    # 引数:
    # - payload: 保存する状態情報(dict)
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def stop_old_channel(service, state):
    # ------------------------------------------------------------
    # 旧 watch チャンネルを停止する。
    #
    # 引数:
    # - service: Google Calendar API service
    # - state: 既存状態dict（channel_id/resource_id を含む）
    #
    # 出力:
    # - なし
    #
    # 備考:
    # - 停止対象が無い場合は何もしない
    # - 停止失敗時も更新処理は継続できるよう warning に留める
    # ------------------------------------------------------------
    channel_id = state.get("channel_id")
    resource_id = state.get("resource_id")
    if not channel_id or not resource_id:
        return
    try:
        service.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()
        logger.info("old watch stopped: %s", channel_id)
    except Exception as exc:
        logger.warning("failed to stop old channel: %s", exc)


def main():
    # ------------------------------------------------------------
    # watch 更新のエントリーポイント。
    #
    # 処理概要:
    # 1) 必須envと認証情報を検証
    # 2) 既存状態を読み込み、旧チャンネルを停止
    # 3) 新しい watch を登録
    # 4) レスポンスを状態ファイルへ保存
    #
    # 出力:
    # - 正常終了: 0
    # - 異常終了: SystemExit
    # ------------------------------------------------------------
    if not GOOGLE_CALENDAR_ID:
        raise SystemExit("GOOGLE_CALENDAR_ID is required")
    service = get_calendar_service()
    if not service:
        raise SystemExit("Service account info not found or invalid")

    state = load_state()
    stop_old_channel(service, state)

    channel_id = WATCH_CHANNEL_ID or f"gcal-{uuid.uuid4()}"
    body = build_watch_request(channel_id)
    if not body:
        raise SystemExit("GCAL_WEBHOOK_URL is required")

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
    logger.info("watch renewed: %s", response)


if __name__ == "__main__":
    main()
