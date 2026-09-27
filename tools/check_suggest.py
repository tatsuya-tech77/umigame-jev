"""★1〜2の問題で、質問の候補を全部聞くと要点が全部そろうかを、本物の Jev で確かめる。

    JEV_ENV_FILE=../jev-lab/.env ../jev-lab/.venv/bin/python -m tools.check_suggest [--runs 3]

画面と同じ順（puzzles.suggestions）に全部聞き、そのあとの progress() が全要点 0.6 以上なら OK。
キャッシュは使わない（確率の揺れも含めて確かめるため、複数回聞く）。
"""

from __future__ import annotations

import argparse
import asyncio

from umigame import judge, puzzles
from umigame.client import close_client, make_client

MARGIN = 0.6  # 判定のしきい値（0.5）より少し上。揺れても候補が出るように


async def run(jev, p: dict) -> tuple[list[tuple[str, str]], list[float]]:
    hist: list[tuple[str, str]] = []
    for q in puzzles.suggestions(p):
        r = await judge.answer(jev, p, q, hist)
        if r.label in judge.ANSWER_LABELS:
            hist.append((q, r.label))
    pr = await judge.progress(jev, p, hist)
    return hist, list(pr.keys.values())


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args()
    jev = make_client()
    bad = 0
    try:
        for p in puzzles.PUZZLES:
            if p.get("level", 3) > 2:
                continue
            for i in range(a.runs):
                hist, keys = await run(jev, p)
                if i == 0:
                    for q, ans in hist:
                        print(f"  [{p['id']}] {q} → {ans}")
                ok = all(k >= MARGIN for k in keys)
                bad += not ok
                print(f"{'✓' if ok else '✗'} {p['id']} {i + 1}回目: 要点 {[round(k, 2) for k in keys]}")
    finally:
        await close_client(jev)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
