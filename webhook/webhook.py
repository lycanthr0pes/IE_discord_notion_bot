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

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_EVENT_INTERNAL_DB_ID = os.getenv("NOTION_EVENT_INTERNAL_ID")

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_JSON_PATH = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SYNC_STATE_FILE = "gcal_sync_state.json" # カレンダー情報ローカル保存
JST = timezone(timedelta(hours=9))

headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

_calendar_service = None


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
    # Google Calendar API クライアントを作成する。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - 成功: Calendar API service オブジェクト
    # - 失敗: None
    #
    # 備考:
    # 初回生成後は _calendar_service を返し続ける。
    # ------------------------------------------------------------
    # Google Calendar APIクライアントを作成する
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service
    if not GOOGLE_CALENDAR_ID:
        return None
    info = load_service_account_info()
    if not info:
        return None
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    _calendar_service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _calendar_service


def load_sync_state():
    # ------------------------------------------------------------
    # 差分同期の基準時刻(updated_min)をローカル状態ファイルから読み込む。
    #
    # 引数:
    # - なし
    #
    # 出力:
    # - 成功: 状態dict
    # - 失敗/未作成: 空dict {}
    # ------------------------------------------------------------
    # 前回の更新時刻(updatedMin)をローカルに保存して差分取得する
    if not os.path.exists(SYNC_STATE_FILE):
        return {}
    try:
        with open(SYNC_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_sync_state(updated_min):
    # ------------------------------------------------------------
    # 次回の差分同期で使う updated_min を状態ファイルへ保存する。
    #
    # 引数:
    # - updated_min: ISO日時文字列
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # 次回の差分取得に使うupdatedMinを保存
    with open(SYNC_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated_min": updated_min}, f, ensure_ascii=False, indent=2)


def list_updated_events(updated_min):
    # ------------------------------------------------------------
    # updated_min 以降に更新された Google Calendar イベントを取得する。
    #
    # 引数:
    # - updated_min: 差分取得の基準時刻（ISO文字列）
    #
    # 出力:
    # - 成功: イベント配列 list[dict]
    # - 失敗/未設定: 空配列 []
    #
    # 備考:
    # - 初回は直近30日を対象にする
    # - showDeleted=True で削除イベントも含める
    # - nextPageToken を辿って全件取得する
    # ------------------------------------------------------------
    # 更新時刻を基準にカレンダーイベントを差分取得（削除も含む）
    service = get_calendar_service()
    if not service or not GOOGLE_CALENDAR_ID:
        return []

    if not updated_min:
        lookback = datetime.now(timezone.utc) - timedelta(days=30)
        updated_min = lookback.isoformat()

    events = []
    page_token = None
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

    return events


def build_notion_date(event):
    # ------------------------------------------------------------
    # Googleイベントの日時情報を Notion date プロパティ形式へ変換する。
    #
    # 引数:
    # - event: Google Calendar イベントJSON
    #
    # 出力:
    # - 成功: {"start": ..., "end": ...} 形式のdict
    # - 失敗: None（startが取得できない場合）
    # ------------------------------------------------------------
    # Googleイベントの日時をNotionのdate形式に変換
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
    # ------------------------------------------------------------
    # GoogleイベントIDをキーに内部用Notionページを1件検索する。
    #
    # 引数:
    # - google_event_id: Google Calendar のイベントID
    #
    # 出力:
    # - 見つかった場合: Notionページdict
    # - 見つからない/失敗: None
    # ------------------------------------------------------------
    # GoogleイベントIDで内部用Notionページを検索
    if not NOTION_EVENT_INTERNAL_DB_ID:
        return None

    url = f"https://api.notion.com/v1/databases/{NOTION_EVENT_INTERNAL_DB_ID}/query"
    data = {
        "filter": {
            "property": "GoogleイベントID",
            "rich_text": {"equals": google_event_id},
        }
    }
    res = requests.post(url, headers=headers, json=data)
    if res.status_code != 200:
        return None
    results = res.json().get("results", [])
    return results[0] if results else None


def notion_create_event(name, content, date_prop, creator_id, event_url, google_event_id):
    # ------------------------------------------------------------
    # 内部用Notion DB にイベントページを新規作成する。
    #
    # 引数:
    # - name: イベント名
    # - content: イベント内容
    # - date_prop: Notion date 形式の日時dict
    # - creator_id: 作成者識別子
    # - event_url: GoogleイベントURL
    # - google_event_id: GoogleイベントID
    #
    # 出力:
    # - 成功: 作成したページID(str)
    # - 失敗: None
    #
    # 備考:
    # 作成後にページID列を埋めるため notion_update_event(..., page_uuid=page_id) を実行する。
    # ------------------------------------------------------------
    # 内部用Notionにイベントページを新規作成
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
            "GoogleイベントID": {
                "rich_text": [{"text": {"content": str(google_event_id)}}]
            },
        },
    }
    res = requests.post(url, headers=headers, json=data)
    if res.status_code not in (200, 201):
        logger.error("Notion作成エラー: %s", res.text)
        return None

    page_id = res.json()["id"]
    notion_update_event(page_id, page_uuid=page_id)
    return page_id


def notion_update_event(
    page_id,
    name=None,
    content=None,
    date_prop=None,
    event_url=None,
    google_event_id=None,
    page_uuid=None,
):
    # ------------------------------------------------------------
    # 内部用Notionイベントページを部分更新する。
    #
    # 引数:
    # - page_id: 更新対象ページID
    # - name/content/date_prop/event_url/google_event_id/page_uuid:
    #   None 以外の項目のみ更新する
    #
    # 出力:
    # - 成功: True
    # - 失敗: False
    # ------------------------------------------------------------
    # 内部用Notionページを更新
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
        props["GoogleイベントID"] = {
            "rich_text": [{"text": {"content": str(google_event_id)}}]
        }
    if page_uuid is not None:
        props["ページID"] = {"rich_text": [{"text": {"content": str(page_uuid)}}]}

    res = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=headers,
        json={"properties": props},
    )
    return res.status_code in (200, 201)


def notion_archive_by_google_event_id(google_event_id):
    # ------------------------------------------------------------
    # GoogleイベントIDに対応する Notion ページをアーカイブする。
    #
    # 引数:
    # - google_event_id: Google Calendar のイベントID
    #
    # 出力:
    # - 成功: True
    # - 失敗/対象なし: False
    # ------------------------------------------------------------
    # Googleイベントが削除された場合はNotion側をアーカイブ
    page = notion_find_by_google_event_id(google_event_id)
    if not page:
        return False
    res = requests.patch(
        f"https://api.notion.com/v1/pages/{page['id']}",
        headers=headers,
        json={"archived": True},
    )
    return res.status_code in (200, 201)


def upsert_event_to_notion(event):
    # ------------------------------------------------------------
    # Googleイベント1件を Notion 内部DBへ反映する（作成/更新/削除）。
    #
    # 引数:
    # - event: Google Calendar イベントJSON
    #
    # 処理概要:
    # 1) id を取得できない場合はスキップ
    # 2) status=cancelled は Notion をアーカイブ
    # 3) 既存ページがあれば更新、無ければ新規作成
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # GoogleイベントをNotion内部DBへ反映（作成/更新/削除）
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
        notion_update_event(
            page["id"],
            name=name,
            content=content,
            date_prop=date_prop,
            event_url=event_url,
            google_event_id=google_event_id,
        )
        return

    notion_create_event(
        name=name,
        content=content,
        date_prop=date_prop,
        creator_id=creator_id,
        event_url=event_url,
        google_event_id=google_event_id,
    )


def sync_calendar_to_notion():
    # ------------------------------------------------------------
    # 差分同期のメイン処理。Google Calendar から Notion へ反映する。
    #
    # 処理概要:
    # 1) 必須環境変数を検証
    # 2) state から updated_min を読み込み
    # 3) 更新イベントを差分取得
    # 4) 各イベントを upsert_event_to_notion() で反映
    # 5) 現在時刻を次回基準として保存
    #
    # 出力:
    # - なし
    # ------------------------------------------------------------
    # Pub/Sub通知をトリガーに差分同期(作成/更新/削除)を実行
    if not (NOTION_TOKEN and NOTION_EVENT_INTERNAL_DB_ID and GOOGLE_CALENDAR_ID):
        logger.error("必要な環境変数が不足しています")
        return

    state = load_sync_state()
    updated_min = state.get("updated_min")

    events = list_updated_events(updated_min)
    for event in events:
        upsert_event_to_notion(event)

    now_iso = datetime.now(timezone.utc).isoformat()
    save_sync_state(now_iso)


@app.route("/gcal/webhook", methods=["POST"])
def gcal_webhook():
    # ------------------------------------------------------------
    # Pub/Sub push を受けるWebhookエンドポイント。
    #
    # 処理概要:
    # - 受信をトリガーに sync_calendar_to_notion() を実行
    #
    # 出力:
    # - HTTP 204 No Content
    # ------------------------------------------------------------
    # Pub/Sub pushのJSONはトリガー用途として受け取り、差分同期(作成/更新/削除)を実行
    sync_calendar_to_notion()
    return "", 204


@app.route("/health", methods=["GET"])
def health():
    # ------------------------------------------------------------
    # 稼働確認用のヘルスチェックエンドポイント。
    #
    # 出力:
    # - "ok", 200
    # ------------------------------------------------------------
    return "ok", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
