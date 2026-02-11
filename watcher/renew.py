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
    # ------------------------------------------------------------
    # Google Service Account の認証情報(JSON/dict)を読み込んで返す。
    #
    # 引数:
    # - なし（環境変数を参照）
    # - GOOGLE_SERVICE_ACCOUNT_JSON
    #   1) JSON文字列そのもの
    #   2) JSONファイルのパス
    # - GOOGLE_SERVICE_ACCOUNT_JSON_PATH
    #   明示的なJSONファイルパス（フォールバック）
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
    # ------------------------------------------------------------
    # Calendar APIクライアントを構築
    info = load_service_account_info()
    if not info:
        return None
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def build_watch_request(channel_id):
    # ------------------------------------------------------------
    # Calendar watch 再登録用のリクエストボディを組み立てる。
    #
    # 引数:
    # - channel_id: watch チャンネルID（一意文字列）
    #
    # 出力:
    # - 成功: watch request dict
    # - 失敗: None
    #
    # 備考:
    # GCAL_PUBSUB_TOPIC がある場合は Pub/Sub 経由、
    # 無い場合は GCAL_WEBHOOK_URL 直通知で構成する。
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


def load_state():
    # ------------------------------------------------------------
    # 直近の watch チャンネル状態をローカルファイルから読み込む。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - 成功: 状態dict（channel_id/resource_id など）
    # - 失敗/未作成: 空dict {}
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
    # 次回更新で利用する watch 状態をローカルファイルへ保存する。
    #
    # 引数:
    # - payload: channel_id / resource_id / expiration などの情報
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # 次回の更新に使うチャンネル情報を保存
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def stop_old_channel(service, state):
    # ------------------------------------------------------------
    # 前回登録済みの watch チャンネルを停止する。
    #
    # 引数:
    # - service: Calendar API service オブジェクト
    # - state: load_state() で取得した状態dict
    #
    # 出力:
    # - なし
    #
    # 備考:
    # channel_id と resource_id が両方ある場合のみ channels.stop を実行する。
    # 停止失敗時は警告ログを出し、処理自体は継続する。
    # ------------------------------------------------------------
    # 以前のチャンネルIDとresourceIdがあれば停止する
    channel_id = state.get("channel_id")
    resource_id = state.get("resource_id")
    if not channel_id or not resource_id:
        return
    try:
        service.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()
        print("old watch stopped:", channel_id)
    except Exception as exc:
        print("warn: failed to stop old channel:", exc)


def main():
    # ------------------------------------------------------------
    # Google Calendar watch を更新するエントリポイント。
    #
    # 処理概要:
    # 1) 必須設定（カレンダーID/認証情報）を検証
    # 2) 保存済み state から旧チャンネルを停止
    # 3) 新しい watch を再登録
    # 4) 返却された新チャンネル情報を state に保存
    #
    # 出力:
    # - なし（watch 再登録・状態更新）
    # ------------------------------------------------------------
    # watchを再登録（Cronで定期実行）
    if not GOOGLE_CALENDAR_ID:
        raise SystemExit("GOOGLE_CALENDAR_ID is required")

    service = get_calendar_service()
    if not service:
        raise SystemExit("Service account info not found or invalid")

    state = load_state()
    stop_old_channel(service, state)

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
    print("watch renewed:", response)


if __name__ == "__main__":
    main()
