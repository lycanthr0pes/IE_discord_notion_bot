# IE Discord Notion Bot

このリポジトリは以下の 2 つの連携を提供します。

1. Discord Scheduled Event -> Google Calendar / Notion
2. Google Calendar -> Notion（Webhook 直送）

## Workflow

### 1) Discord -> Google Calendar / Notion

- `bot/bot.py` が Discord Scheduled Event を検知
- Google Calendar にイベントを作成
- Notion（内部/外部DB）へイベントを反映

### 2) Google Calendar -> Notion（Webhook-only）

- `watcher/register.py` または `watcher/renew.py` が Google Calendar watch を登録
- 通知先は `GCAL_WEBHOOK_URL`（Webhook 直送）
- `webhook/webhook.py` が `/gcal/webhook` を受信
- 受信時に Google Calendar の更新イベントを取得し、Notion 内部DBを upsert

## Services

- `bot`:
  - Discord 連携本体
- `watcher`:
  - watch 登録/更新ジョブ
- `webhook`:
  - Google Calendar 通知受信 + Notion 同期

## Command Features

### Discord Slash Commands

- `/q_answer`
  - 未回答の質問を選択して回答を登録
  - 実行チャンネルは `QA_CHANNEL_ID` で制限
- `/q_edit`
  - 回答済みの質問を選択して回答を編集
  - 実行チャンネルは `QA_CHANNEL_ID` で制限

### Bot Event Sync

- Discord Scheduled Event 作成時
  - Google Calendar にイベント作成
  - Notion 外部/内部 DB に登録
- Discord Scheduled Event 更新時
  - Notion 外部/内部 DB を更新
- Discord Scheduled Event 削除時
  - Notion 外部/内部 DB をアーカイブ

### Scheduled Tasks

- `auto_clean`（24時間ごと）
  - 古いイベントを Notion からアーカイブ
- `auto_check_qa`（6時間ごと）
  - Q&A DB の差分を検知して未回答を通知
- `auto_day_before_reminder`（10分ごと）
  - 開始24時間前の Discord イベントを検知しメンション通知
  
## Operation

1. `webhook` をデプロイして URL を確定
2. `watcher` の `GCAL_WEBHOOK_URL` に `https://<webhook-domain>/gcal/webhook` を設定
3. `watcher/register.py` を 1 回実行して watch を初期登録
4. 定期的に `watcher/renew.py` を実行して watch を更新

## Health Check

- `GET /health` -> `ok`
- `GET/POST /gcal/sync` -> 手動同期
