"""コマンドで1手ずつ遊ぶ。Claude Code のサブエージェントを AI プレイヤーにするときに使う。

    python -m umigame.play_cli start  --player claude-opus --pid window
    python -m umigame.play_cli ask    --player claude-opus --pid window "女は眠っていましたか？"
    python -m umigame.play_cli guess  --player claude-opus --pid window "…だから"
    python -m umigame.play_cli status --player claude-opus --pid window

ルールは session.py（人間の画面・OpenRouter の対戦と同じ）。真相は表示しない。
進行は results/sessions/<player>--<pid>.json、終わったゲームは results/arena-claude-code.jsonl に1行で残す。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import puzzles
from .client import PROJECT_ROOT, close_client, make_client
from .session import MAX_GUESSES, MAX_QUESTIONS, MAX_UNCOUNTED, METER_EVERY, METER_FROM_LEVEL, AnswerCache, Session

RESULTS = PROJECT_ROOT / "results"
SESSIONS = RESULTS / "sessions"
CACHE = RESULTS / "answer_cache.json"
RECORD = RESULTS / "arena-claude-code.jsonl"

RULES = f"""ルール:
- 「はい／いいえ」で答えられる質問を1つずつして、真相を突き止める。出題者は「はい」「いいえ」「関係ありません」「一部は合っていますが、違うところもあります」「どちらとも言えません」のどれかで答える
- 質問は最大{MAX_QUESTIONS}回。「どちらとも言えません」と、質問になっていない入力は回数に数えない（合わせて{MAX_UNCOUNTED}回まで）
- 解答は最大{MAX_GUESSES}回。外すと +2。惜しいときは、おさえた要点とまだの要点の名前が表示される。候補を並べた解答は不正解
- 手数 = 数えた質問の回数 + 外した解答 × 2（少ないほど良い）
- ★{METER_FROM_LEVEL}以上の問題では、数えた質問{METER_EVERY}回ごとに到達度（遠い／近い／あと少し／そろった）が表示される。「そろった」は要点を全部突き止めたという意味
- できるだけ少ない回数で解くこと"""


def _path(player: str, pid: str, run: int) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in player)
    return SESSIONS / f"{safe}--{pid}--{run}.json"


def _status(s: Session) -> str:
    parts = [f"数えた質問 {s.questions}/{MAX_QUESTIONS}", f"数えない入力 {s.uncounted}/{MAX_UNCOUNTED}",
             f"解答 {s.guesses}/{MAX_GUESSES}", f"成績（今の時点） {s.score}"]
    if s.meter:
        parts.append(f"到達度 {s.meter}")
    return "  ".join(parts)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["start", "ask", "guess", "status"])
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--player", required=True)
    ap.add_argument("--pid", required=True)
    ap.add_argument("--run", type=int, default=1, help="同じプレイヤーが同じ問題を何回目に解くか（1回ごとに別のプレイヤーで）")
    a = ap.parse_args()
    p = puzzles.get(a.pid)
    path = _path(a.player, a.pid, a.run)

    if a.command == "start":
        if path.exists():
            print("このプレイヤーはこの問題をもう始めています。status で状態を見てください。")
            return 1
        SESSIONS.mkdir(parents=True, exist_ok=True)
        s = Session(pid=a.pid, player=a.player)
        path.write_text(s.to_json(), encoding="utf-8")
        print(f"問題（★{p['level']}）: {p['story']}\n\n{RULES}")
        return 0

    if not path.exists():
        print("まだ start していません。")
        return 1
    s = Session.from_json(path.read_text(encoding="utf-8"))

    if a.command == "status":
        print(f"問題（★{p['level']}）: {p['story']}\n{_status(s)}" + ("\nこのゲームは終わっています。" if s.finished else ""))
        for t in s.log:
            mark = "" if t.get("counted", True) else "（数えない）"
            print(f"  {'Q' if t['type'] == 'question' else '解答'}: {t['text']} → {t['answer']}{mark}")
        return 0

    text = a.text.strip()
    if not text:
        print("質問（または解答）の文を渡してください。")
        return 1
    jev = make_client()
    try:
        if a.command == "ask":
            if not s.can_ask:
                print("もう質問できません。解答してください。")
                return 1
            t = await s.ask(jev, p, text[:120], AnswerCache(CACHE))
            note = "" if t["counted"] else "（回数に数えません）"
            print(f"{t['answer']}{note}")
            if "meter" in t:
                print(f"到達度: {t['meter']}")
        else:
            t = await s.guess(jev, p, text[:300])
            print(t["answer"])
    finally:
        await close_client(jev)

    path.write_text(s.to_json(), encoding="utf-8")
    print(_status(s))
    if s.finished:
        print("ゲーム終了。" + ("解けました。" if s.solved else "解けませんでした。"))
        rec = {"model": s.player, "pid": s.pid, "run": a.run, "level": p["level"], "solved": s.solved, "score": s.score,
               "questions": s.questions, "uncounted": s.uncounted, "guesses": s.guesses,
               "wrong_guesses": s.wrong_guesses, "jev_tokens": s.jev_tokens,
               "seconds": round(s.ended - s.started), "via": "claude-code", "log": s.log}
        with RECORD.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
