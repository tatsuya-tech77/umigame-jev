"""Jev の出題者としての精度を測る。

    .venv/bin/python -m umigame.evaluate                 # 本物の API を使う
    .venv/bin/python -m umigame.evaluate --compare-facts # 事実の一覧なしの条件も測る

- 質問への答え: puzzles.py の gold と比べた正答率、はい⇄いいえの取り違え
- 質問ではない入力（ヒント要求・指示・複合質問）を定型文に回せるか
- 最終解答の判定: 正解・不正解・候補の並べ立てを正しく見分けられるか
- 1回あたりのトークン数と待ち時間（費用の見積もり用）

注意: しきい値や facts はこの gold を見ながら調整したので、ここの数字は楽観的に出る。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from collections import Counter

from . import judge
from .client import close_client, make_client
from .puzzles import META_INPUTS, PUZZLES

PRICE_PER_1M = 0.042
LABELS = ["yes", "no", "irrelevant"]


async def run_answers(client, with_facts: bool):
    jobs, meta = [], []
    for p in PUZZLES:
        pp = p if with_facts else {**p, "facts": []}
        for i, (q, gold) in enumerate(p["gold"]):
            # 遊んでいるときと同じく、それまでの質問（正しい答え付き）を直前のやりとりとして渡す
            jobs.append(judge.answer(client, pp, q, p["gold"][:i]))
            meta.append((p["id"], q, gold))
    replies = await judge.gather_limited(jobs)
    return list(zip(meta, replies))


def report_answers(name, rows):
    n = len(rows)
    raw_ok = sum(1 for (_, _, g), r in rows if max(r.probabilities, key=r.probabilities.get) == g)
    answered = [(m, r) for m, r in rows if r.label != "unsure"]
    ok = sum(1 for (_, _, g), r in answered if r.label == g)
    # 一番まずい間違い: yes と no の取り違え（プレイヤーを嘘で迷わせる）
    flip = sum(1 for (_, _, g), r in answered if {g, r.label} == {"yes", "no"})
    print(f"\n=== {name} ===")
    print(f"  そのまま答えた場合の正答率      {raw_ok}/{n} = {raw_ok/n:.0%}")
    print(f"  ロジックを通した後: 答えた {len(answered)}/{n}件, "
          f"うち正解 {ok}/{len(answered)} = {ok/max(1,len(answered)):.0%}")
    print(f"  はい⇄いいえ の取り違え         {flip}件")
    conf = Counter((g, r.label) for (_, _, g), r in rows)
    print("  正解＼Jev   " + "  ".join(f"{l:>10}" for l in LABELS + ["unsure"]))
    for g in LABELS:
        print(f"  {g:>10}  " + "  ".join(f"{conf[(g, l)]:>10}" for l in LABELS + ["unsure"]))
    return raw_ok / n


def show_misses(rows):
    print("\n  --- 外れ・保留 ---")
    for (pid, q, g), r in rows:
        top = max(r.probabilities, key=r.probabilities.get)
        if top != g or r.label == "unsure":
            probs = " ".join(f"{k[:3]}={v:.2f}" for k, v in r.probabilities.items())
            print(f"  [{pid}] {q}  正解={g}  Jev={r.label}({r.confidence:.2f})  {probs}")


async def main():
    client = make_client()
    try:
        facts_rows = await run_answers(client, with_facts=True)
        report_answers("質問への答え", facts_rows)
        show_misses(facts_rows)
        if "--compare-facts" in sys.argv:
            report_answers("質問への答え（事実の一覧なし）", await run_answers(client, with_facts=False))

        print("\n=== 質問ではない入力 ===")
        p0 = PUZZLES[0]
        metas = await judge.gather_limited([judge.answer(client, p0, m) for m in META_INPUTS])
        caught = 0
        for m, r in zip(META_INPUTS, metas):
            hit = r.label not in judge.ANSWER_LABELS
            caught += hit
            print(f"  {'✓' if hit else '✗'} {m} → {r.text}")
        print(f"  → {caught}/{len(META_INPUTS)} を定型文に回した")

        print("\n=== 最終解答の判定 ===")
        jobs, meta = [], []
        for p in PUZZLES:
            for guess, gold in p["guesses"]:
                jobs.append(judge.judge_guess(client, p, guess))
                meta.append((p["id"], guess, gold))
        verdicts = await judge.gather_limited(jobs)
        ok = strict = 0
        name = {"correct": "正解", "close": "惜しい", "wrong": "違う", "scattershot": "並べ立て"}
        for (pid, guess, gold), v in zip(meta, verdicts):
            good = (v.level in ("correct", "close")) if gold else (v.level not in ("correct", "close"))
            ok += good
            # 厳しい基準: 外れの解答が「惜しい」になるのも失敗、当たりは「正解」でなければ失敗
            strict += (v.level == "correct") if gold else (v.level in ("wrong", "scattershot"))
            keys = " ".join(f"{x:.2f}" for x in v.keys.values())
            decoys = " ".join(f"{x:.2f}" for x in v.decoys.values())
            print(f"  {'✓' if good else '✗'} [{pid}] 期待={'当たり' if gold else '外れ'} 判定={name[v.level]}"
                  f"  要点[{keys}] 外れ仮説[{decoys}]  {guess}")
        print(f"  → {ok}/{len(meta)} 一致（当たり→正解か惜しい、外れ→違うか並べ立て）")
        print(f"  → 厳しい基準 {strict}/{len(meta)}（当たり→正解のみ）")

        print("\n=== 進み具合メーター（質問が進むと上がるか） ===")
        for p in PUZZLES[:2]:
            hist, line = [], []
            for q, g in [(q, g) for q, g in p["gold"] if g == "yes"]:
                hist.append((q, g))
                pr = await judge.progress(client, p, hist)
                line.append(f"{pr.ratio:.0%}")
            print(f"  [{p['id']}] 『はい』の質問を1つずつ足すと: {' → '.join(line)}")

        toks = [r.input_tokens for _, r in facts_rows]
        ms = [r.ms for _, r in facts_rows]
        print("\n=== 費用・速さ（事実の一覧あり、質問1回） ===")
        print(f"  入力トークン 平均 {statistics.mean(toks):.0f}  "
              f"→ 1回 ${statistics.mean(toks)*PRICE_PER_1M/1e6:.7f}")
        print(f"  待ち時間 中央値 {statistics.median(ms):.0f}ms / 最大 {max(ms):.0f}ms"
              f"（{len(ms)}件を16並列で投げたときの値）")
        ptoks = [v.input_tokens for v in verdicts]
        print(f"  最終解答の判定 平均 {statistics.mean(ptoks):.0f} トークン")
    finally:
        await close_client(client)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
