"""確率から答えを決めるやり方（judge.decide とその変え方）を、同じデータで比べる。

    # 1) Jev に聞いて確率を集める（前のやりとりあり／なしの両方。追加のチェックもまとめて聞く）
    JEV_ENV_FILE=../jev-lab/.env ../jev-lab/.venv/bin/python -m tools.eval_decide collect
    # 2) 集めた確率に、いろいろな決め方を当てはめて比べる（API は呼ばない）
    ../jev-lab/.venv/bin/python -m tools.eval_decide compare

評価に使うのは2つ。
- 確認用の質問（puzzles.py の gold、70問）… 簡単で、どの決め方でも差が出にくい
- 難しい評価セット（results/eval/hard_set.json、86問）… AI の対戦で実際に出た紛らわしい質問に、
  真相と照らして「受け入れられる答え」を付けたもの。ゲームの問題の中身が入るので Git には入れない

指標は、正解（受け入れられる答えを出した数）、違うのに「はい」、本当は「はい」なのに否定、濁した数。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from typesafe_sdk import Noul

from umigame import judge, puzzles
from umigame.client import PROJECT_ROOT, close_client, make_client

EVAL = PROJECT_ROOT / "results" / "eval"
HARD = EVAL / "hard_set.json"
ITEMS = EVAL / "eval_items.json"

# 今の作りにはない、候補のチェック（同じリクエストに足す）
EXTRA = {
    "presup": Noul(instructions="プレイヤーの質問が当然のこととして前提にしている内容（「その〜」が指すもの、決めつけている事情など）の中に、真相や事実の一覧と食い違うものがあるか"),
    "alltrue": Noul(instructions="プレイヤーの質問に含まれる内容は、すべて真相や事実の一覧どおりか（一つでも違う部分があれば当てはまらない）"),
    "sometrue": Noul(instructions="プレイヤーの質問に含まれる内容のうち、少なくとも一部は真相や事実の一覧どおりか"),
}


async def _ask(client, p: dict, q: str, hist) -> dict:
    extra = {judge.HISTORY_KEY: judge._history(hist[-judge.HISTORY_TURNS:])} if hist else {}
    extra["プレイヤーの質問"] = q
    qs = {"answer": judge.ANSWER_QUESTION, "kind": judge.INPUT_KIND, "contradicts": judge.CONTRADICTS, **EXTRA}
    a = (await client.system_one(judge._state(p, **extra), qs)).answers
    return {"probs": {k: float(v) for k, v in a["answer"].probabilities.items()},
            "kind": {k: float(v) for k, v in a["kind"].probabilities.items()},
            **{k: float(a[k].noul) for k in ("contradicts", *EXTRA)}}


async def collect() -> None:
    items = [{"set": "hard", **it} for it in json.loads(HARD.read_text())]
    for p in puzzles.PUZZLES:
        for i, (q, g) in enumerate(p["gold"]):
            items.append({"set": "gold", "pid": p["id"], "q": q, "hist": p["gold"][:i], "ok": [g]})
    client = make_client()
    try:
        jobs = []
        for it in items:
            p = puzzles.get(it["pid"])
            jobs += [_ask(client, p, it["q"], it["hist"]), _ask(client, p, it["q"], [])]
        res = await judge.gather_limited(jobs)
    finally:
        await close_client(client)
    for k, it in enumerate(items):
        it["with"], it["without"] = res[2 * k], res[2 * k + 1]
    ITEMS.write_text(json.dumps(items, ensure_ascii=False))
    print(f"{ITEMS}: {len(items)}件")


# ---------------------------------------------------------------- 決め方
def _gate(r):
    k = r["kind"]
    top = max(k, key=k.get)
    return top if top != "question" and k[top] >= judge.NOT_QUESTION_ABOVE else None


def _base(r, yes_above=0.5, not_yes_below=0.3):
    py = r["probs"].get("yes", 0.0)
    if py >= yes_above:
        return "yes"
    if py <= not_yes_below:
        return "no" if r["probs"]["no"] >= r["probs"]["irrelevant"] else "irrelevant"
    return "unsure"


def rule(yes_above=0.5, not_yes_below=0.3, check="contradicts", check_src="with", check_above=0.5):
    """今の decide() と同じ形で、しきい値と「一部」の判定に使うチェックを差し替えられる決め方。"""
    def decide(it):
        r = it["with"]
        g = _gate(r)
        if g:
            return g
        b = _base(r, yes_above, not_yes_below)
        if b != "yes" or not check:
            return b
        v = it[check_src][check]
        hit = v < check_above if check == "alltrue" else v >= check_above
        return "partly" if hit else "yes"
    return decide


def argmax(it):
    r = it["with"]
    return _gate(r) or max(r["probs"], key=r["probs"].get)


METHODS = [
    ("一番高い確率そのまま（if 文なし）", argmax),
    ("今の作り（0.5/0.3＋食い違い）", rule()),
    ("帯を広げる（0.7/0.3）", rule(yes_above=0.7)),
    ("下を0.1に", rule(not_yes_below=0.1)),
    ("食い違いを前のやりとりなしで", rule(check_src="without")),
    ("食い違いを前のやりとりなしで・0.4", rule(check_src="without", check_above=0.4)),
    ("前提チェック（前のやりとりなし）", rule(check="presup", check_src="without")),
    ("「全部真相どおりか」で一部判定", rule(check="alltrue")),
]


def compare() -> None:
    items = json.loads(ITEMS.read_text())
    for name in ("hard", "gold"):
        rows = [it for it in items if it["set"] == name]
        print(f"\n== {name}（{len(rows)}問）")
        print(f"{'決め方':34s} 正解  違うのに「はい」  「はい」なのに否定  濁した")
        for label, fn in METHODS:
            ok = false_yes = missed_yes = unsure = 0
            for it in rows:
                got, good = fn(it), set(it["ok"])
                ok += got in good
                false_yes += got == "yes" and "yes" not in good
                missed_yes += got in ("no", "irrelevant") and "yes" in good and not good & {"no", "irrelevant"}
                unsure += got == "unsure"
            print(f"{label:34s} {ok:3d}   {false_yes:8d}          {missed_yes:8d}        {unsure:4d}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["collect", "compare"])
    a = ap.parse_args()
    if a.command == "collect":
        asyncio.run(collect())
    else:
        compare()


if __name__ == "__main__":
    main()
