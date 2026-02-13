# IE Discord Notion Bot

Discord と Google Calendar / Notion を同期する Bot です。

## Workflow

### 1) Discord -> Google Calendar / Notion

- `bot/bot.py` が Discord Scheduled Event を監視
- イベント作成時:
  - Google Calendar にイベント作成
  - Notion 外部DB / 内部DB に登録
- イベント更新時:
  - Notion 外部DB / 内部DB を更新
  - 内部DBに保存済みの GoogleイベントID があれば Google Calendar も更新
- イベント削除時:
  - Notion 外部DB / 内部DB をアーカイブ
  - 内部DBに保存済みの GoogleイベントID があれば Google Calendar も削除

### 2) Google Calendar -> Discord / Notion (webhook-only)

- `watcher/register.py` / `watcher/renew.py` が Google Calendar watch を管理
  - 通知先は `GCAL_WEBHOOK_URL`（`https://<webhook-domain>/gcal/webhook`）
- Notion:
  - `webhook/webhook.py` が通知受信後に Google Calendar の差分を取得して Notion に反映
- Discord:
  - `webhook/webhook.py` が通知受信後に Google Calendar の差分を取得して Discord に反映

## Services

- `bot`: Discord Bot 本体
- `watcher`: Google Calendar watch 登録 / 更新
- `webhook`: Google Calendar 通知受信 + Notion / Discord 反映

## Command Features

### Discord Slash Commands

- `/q_answer`
  - 未回答の質問を選んで回答を投稿
  - 投稿先チャンネルは `QA_CHANNEL_ID`
- `/q_edit`
  - 既存回答を選んで編集
  - 投稿先チャンネルは `QA_CHANNEL_ID`

### Bot Event Sync

- Discord イベント作成:
  - Google Calendar 作成
  - Notion 外部DB / 内部DB 作成
- Discord イベント更新:
  - Notion 外部DB / 内部DB 更新
  - 必要に応じて Google Calendar 更新
- Discord イベント削除:
  - Notion 外部DB / 内部DB アーカイブ

### Scheduled Tasks

- `auto_clean`:
  - 古いイベントを Notion からアーカイブ
- `auto_check_qa`:
  - Q&A DB の差分確認と未回答通知
- `auto_day_before_reminder`:
  - 前日リマインド送信

## Operation

1. `webhook` をデプロイして URL を確定
2. `watcher` の `GCAL_WEBHOOK_URL` に webhook URL を設定
3. `watcher/register.py` を実行して watch 初回登録
4. 定期的に `watcher/renew.py` を実行して watch 更新

## Health Check

- `GET /health` -> `ok`
- `GET /gcal/sync` -> 手動同期
- `POST /gcal/sync` -> 手動同期

## Troubleshooting

### 1) webhook にリクエストが届いているか確認

- `webhook` ログに `/gcal/webhook` アクセスが出るか確認
- まず疎通確認:
  - `curl -i https://<webhook-domain>/health`
  - `curl -i https://<webhook-domain>/gcal/sync`

### 2) 手動同期で切り分ける

- `GET /gcal/sync` が成功して Notion / Discordが更新される場合:
  - Notion API / Google API / 認証は概ね正常
  - 問題は「通知経路（watch -> webhook）」に絞れる

### 3) `updatedMinTooLongAgo` (HTTP 410) が出る場合

- ログに `updatedMinTooLongAgo` が出たら古い同期状態
- 状態ファイルをリセットして再同期

### 4) Notion / Disocrd に反映されない場合

- `webhook` ログで以下を確認:
  - `Google events fetched: N`
  - `Sync completed`
  - Notion API エラー有無

