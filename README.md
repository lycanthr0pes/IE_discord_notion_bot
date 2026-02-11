# ワークフロー

## 1. Discordイベント → Notion外部用/内部用
- Discordでイベント作成
- Botがイベント作成を検知
- 外部用DBには定例会以外を登録
- 内部用DBには定例会も含めて登録（イベントURL付き）

## 2. Discordイベント → Googleカレンダー
- Discordイベント作成時、BotがGoogleカレンダーへ予定を作成
- 返却されたGoogleイベントIDを内部用DBに保存

## 3. Googleカレンダー → Notion内部用
- Calendar watch が変更を検知
- Pub/SubからWebhookへ通知
- Webhookが差分取得し、Notion内部用DBへ作成/更新/アーカイブ

## 4. イベント更新/削除の同期
- Discordイベント更新時はNotion外部用/内部用を更新
- Discordイベント削除時はNotion外部用/内部用をアーカイブ

## 5. 内部用DBの自動アーカイブ
- イベント終了時刻を過ぎたら内部用DBをアーカイブ
- 外部用DBは30日以上前のイベントをアーカイブ

## 6. Q&A機能
- NotionのQ&A DBを監視して新規/更新を検知
- 未回答の質問だけ通知
- Slashコマンドで回答・編集
- 回答保存/更新時に本人だけ見える形で再送

## 7. 前日メンション機能（Discord）
- Botが定期的にDiscordのScheduled Eventを確認
- 「開始24時間前〜24時間前+指定分」のイベントを検出
- 指定チャンネルで指定ロールへメンション通知
- 送信済みイベントはキャッシュで管理し、重複通知を防止
