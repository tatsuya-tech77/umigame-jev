# 公開の手順（Google Cloud Run・東京）

Cloud Run（asia-northeast1）に**最大1台**で置く。誰も来ないと止まり、次のアクセスで数秒で起きる。
1日の利用料の記録は Cloud Storage のバケットを `/data` につないで置く（止まっても消えない）。
設定は `Dockerfile`・`.gcloudignore`。Jev のキーは Secret Manager。

| もの | 名前 |
|---|---|
| プロジェクト | `umigame-jev-2026` |
| リージョン | `asia-northeast1`（東京） |
| Cloud Run サービス | `umigame` |
| バケット（利用料の記録） | `gs://umigame-jev-2026-data` |
| シークレット（Jev のキー） | `typesafe-api-key` |
| 予算アラート | 月500円（50%・100%でメール。自動では止まらない） |

## なぜ最大1台か

IP ごとの回数制限と答えのキャッシュは、サーバーのメモリに持っている。台数が増えると台ごとに別々に数えてしまう。`--max-instances 1` は、アクセスが集まっても費用が1台分を超えない、という守りも兼ねる。
止まるとメモリの回数制限とキャッシュは消えるが、1日の利用料はバケットに残る。

## キーを入れる・替える（画面にも履歴にも残らない）

```bash
read -s "KEY?キーを貼って Enter: " && printf %s "$KEY" | gcloud secrets create typesafe-api-key --data-file=- && unset KEY
```

替えるときは `gcloud secrets create` を `gcloud secrets versions add typesafe-api-key` にする。そのあと再デプロイ（または新しいリビジョン）で読み込み直す。

## デプロイ（初回も更新も同じ）

```bash
gcloud run deploy umigame --source . --region asia-northeast1 \
  --allow-unauthenticated --max-instances 1 --min-instances 0 --memory 512Mi \
  --execution-environment gen2 \
  --add-volume name=data,type=cloud-storage,bucket=umigame-jev-2026-data \
  --add-volume-mount volume=data,mount-path=/data \
  --set-secrets TYPESAFE_API_KEY=typesafe-api-key:latest
```

`--source .` は `.gcloudignore` に従ってアップロードし、Cloud Build で `Dockerfile` からイメージを作る。AI の成績（`results/arena-claude-code.jsonl`）はイメージに入るので、測り直したら再デプロイする。

## 確認

- 表示された URL（`https://umigame-….a.run.app`）を開き、右上の「本日営業中（残り ○%）」が質問するたびに少し減る
- `gcloud run services logs read umigame --region asia-northeast1` に「利用料の記録を保存できません」が出ていない
- `gcloud storage cat gs://umigame-jev-2026-data/budget.json` で今日の記録が見える

## 入れてはいけない設定

- `UMIGAME_SAMPLE_AI`（まだ測っていない AI の仮の成績）
- `UMIGAME_DEMO`（記事用の例題。答えが記事に書いてある）

## 入れている設定（Dockerfile）

| 変数 | 値 | 意味 |
|---|---|---|
| `UMIGAME_BUDGET_FILE` | `/data/budget.json` | 1日の利用料の記録（バケット） |
| `UMIGAME_DAILY_BUDGET_YEN` | `100` | 1日の上限（円）。超えたら日本時間の0時まで休業 |
| `UMIGAME_TRUST_PROXY_HOPS` | `1` | `X-Forwarded-For` の右から何番目を遊んでいる人の IP とみなすか（Cloud Run での並びは初回デプロイで確認） |
| `TYPESAFE_API_KEY` | Secret Manager | Jev のキー。ファイルやコマンドの引数には書かない |
