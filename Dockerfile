# Google Cloud Run（東京・最大1台）で動かす。手順は docs/deploy.md
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
# tzdata: 1日の上限を日本時間で区切るのに使う（slim イメージにはタイムゾーンの情報がないことがある）
RUN pip install --no-cache-dir -r requirements.txt tzdata

COPY umigame ./umigame
# AI の成績だけ入れる。results/ のほかのファイル（評価セット・キャッシュ）は答えが書いてあるので入れない
COPY results/arena-claude-code.jsonl ./results/arena-claude-code.jsonl

# 1日の利用料の記録は /data（Cloud Storage のバケット）に置き、止まっても消えないようにする
ENV HOST=0.0.0.0 \
    PORT=8080 \
    UMIGAME_BUDGET_FILE=/data/budget.json \
    UMIGAME_DAILY_BUDGET_YEN=100 \
    UMIGAME_TRUST_PROXY_HOPS=1

EXPOSE 8080
CMD ["python", "-m", "umigame.web"]
