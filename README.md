# ウミガメのスープ × Jev

水平思考クイズ「ウミガメのスープ」の出題者を、TypeSafe AI の **Jev**（文章を生成しない判定専用モデル）にやらせるブラウザゲーム。

- プレイヤーの質問に Jev が「はい／いいえ／関係ありません」の**確率**を返し、答えはゲームのロジックで決める
- 真相への到達度メーター、ヒント、質問の候補、最終解答の判定（正解／惜しい／違う／候補の並べ立て）
- 問題・真相・ヒント・候補は Claude が下書きし、作者が確認したもの（固定の文章）。プレイ中に AI が文章を生成することはない

遊べるページ: https://umigame-523507334771.asia-northeast1.run.app ／ 解説記事（Qiita）: （URL）

## 問題データについて

このリポジトリに入っている問題は、答えを公開してよいものだけです（★1「3時の目覚まし」と、定番の「ウミガメのスープ」、記事用の例題）。オリジナルの★2〜★4は答えを公開しないので、`umigame/puzzles_private.py`（Git に入れない）に置いています。このファイルがなくても、公開の問題だけで遊べて、テストも通ります。問題の形は `umigame/puzzles.py` の先頭の説明を見てください。

設計（全体の構成・判定のしくみ・API・費用の守り）は [docs/architecture.md](docs/architecture.md) にまとめている。出題者の答え方（確率からどう答えを決めるか）の調査と評価は [docs/research-decide.md](docs/research-decide.md)。

## 動かす

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # TYPESAFE_API_KEY を書く
.venv/bin/python -m umigame.web   # http://127.0.0.1:8789/
```

API を使わずに画面だけ見るときは `JEV_MOCK=1`（答えはでたらめ）。

## 測る・テストする

```bash
.venv/bin/python -m umigame.evaluate                  # 本物の API で精度・費用・速さを測る
.venv/bin/python -m unittest discover -s tests        # ロジックのテスト（API を呼ばない）
```

## 測定結果（2026-09-25、オリジナル5問）

| 項目 | 結果 |
|---|---|
| 質問への答え（70問） | ロジック後 91〜94%、はい⇄いいえの取り違え 0〜1件 |
| 質問ではない入力（ヒント要求・命令・挨拶） | 6/8 を定型文に回した（複合質問は素通り） |
| 最終解答の判定（25例） | 25/25（候補の並べ立て5例はすべて却下） |
| 速さ | 中央値 約0.2秒 |
| 費用 | 質問1回 約1,150トークン＋メーター約850。1ゲーム（30問）約0.4円 |

しきい値と facts はこの確認用の質問を見ながら調整したので、数字は楽観的。

## 公開するとき

`web.py` に、IP ごとの回数制限と「1日の利用料の上限」がある。全員の合計が `UMIGAME_DAILY_BUDGET_YEN`（既定100円）に達すると、その日は店じまいになり、画面に「本日の営業は終了しました」と出る。日本時間の0時に再開する。

回数制限はサーバーのメモリで数える（1分に `UMIGAME_PER_IP_PER_MIN` 回、既定60回）。Render や Cloud Run など中継サーバーの後ろで公開するときは `UMIGAME_TRUST_PROXY_HOPS=1` にする。0 のままだと全員が中継サーバーの IP に見えて、1つの枠を分け合ってしまう。

- 費用の最大値は「上限 × 日数」で決まる（100円なら月に最大約3,000円）
- 使った額は `.budget.json` に保存するので、再起動しても同じ日のうちはリセットされない
- どちらもプロセス1つで動かす前提の簡易版。台数を増やすなら、共有の保存先が必要

## 記事用の画像を撮る

```bash
# デモ用サーバー（例題つき）を 8790 番で起動してから
UMIGAME_DEMO=1 .venv/bin/python -m umigame.web --port 8790
.venv/bin/python -m tools.record_demo --out docs/img   # demo.gif と静止画
```
