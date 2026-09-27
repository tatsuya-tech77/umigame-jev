"""1ゲームの進行とルール（企画書 v5 の3・4節）。人間の画面・AI 対戦・コマンドで同じものを使う。

- 数える質問は最大20回。「どちらとも言えません」と質問になっていない入力は数えない（合わせて10回まで。超えたら数える）
- 解答は最大3回。外すと比べる数に +2（候補の並べ立ても外れ）
- 比べる数 score = 数えた質問 + 外した解答 × 2（小さいほど良い）
- ★3以上は、数えた質問3回ごとに到達度を「遠い／近い／あと少し／そろった」で知らせる
"""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import judge

MAX_QUESTIONS = 20
MAX_UNCOUNTED = 10
MAX_GUESSES = 3
WRONG_GUESS_COST = 2
METER_EVERY = 3
METER_FROM_LEVEL = 3


@dataclass
class Session:
    pid: str
    player: str = ""
    questions: int = 0          # 数えた質問
    uncounted: int = 0          # 数えなかった入力
    guesses: int = 0
    wrong_guesses: int = 0
    solved: bool = False
    meter: str = ""             # 最後に知らせた到達度（★3以上）
    jev_tokens: int = 0
    started: float = field(default_factory=time.time)
    ended: float = 0.0
    log: list[dict] = field(default_factory=list)

    # ---------------------------------------------------------------- 状態
    @property
    def score(self) -> int:
        return self.questions + self.wrong_guesses * WRONG_GUESS_COST

    @property
    def can_ask(self) -> bool:
        return not self.finished and self.questions < MAX_QUESTIONS

    @property
    def finished(self) -> bool:
        return self.solved or self.guesses >= MAX_GUESSES or self.ended > 0

    def history(self) -> list[tuple[str, str]]:
        """Jev に渡す直前のやりとり（答えとして返したものだけ）。"""
        return [(t["text"], t["label"]) for t in self.log
                if t["type"] == "question" and t["label"] in judge.ANSWER_LABELS]

    def give_up(self) -> None:
        if not self.ended:
            self.ended = time.time()

    # ---------------------------------------------------------------- 手
    async def ask(self, jev: Any, p: dict, text: str, cache: "AnswerCache | None" = None) -> dict:
        if not self.can_ask:
            raise ValueError("もう質問できません（解答するか終了してください）")
        key = judge.cache_key(p["id"], text)
        hit = cache.get(key) if cache and key else None
        if hit:
            label, answer = hit
        else:
            r = await judge.answer(jev, p, text, self.history())
            self.jev_tokens += r.input_tokens
            label, answer = r.label, r.text
            if cache and key:
                cache.put(key, (label, answer))
        counted = label in judge.COUNTED_LABELS or self.uncounted >= MAX_UNCOUNTED
        if counted:
            self.questions += 1
        else:
            self.uncounted += 1
        turn = {"type": "question", "text": text, "label": label, "answer": answer, "counted": counted}
        if counted and p.get("level", 3) >= METER_FROM_LEVEL and self.questions % METER_EVERY == 0:
            pr = await judge.progress(jev, p, self.history())
            self.jev_tokens += pr.input_tokens
            self.meter = pr.band
            turn["meter"] = self.meter
        self.log.append(turn)
        return turn

    async def guess(self, jev: Any, p: dict, text: str) -> dict:
        if self.finished:
            raise ValueError("このゲームは終わっています")
        v = await judge.judge_guess(jev, p, text)
        self.jev_tokens += v.input_tokens
        self.guesses += 1
        if v.level == "correct":
            self.solved = True
        else:
            self.wrong_guesses += 1
        turn = {"type": "guess", "text": text, "level": v.level, "hits": v.hits,
                "total": len(v.keys), "answer": verdict_text(v, p)}
        self.log.append(turn)
        if self.finished and not self.ended:
            self.ended = time.time()
        return turn

    # ---------------------------------------------------------------- 保存
    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, s: str) -> "Session":
        return cls(**json.loads(s))


def close_text(v: judge.Verdict, p: dict) -> str:
    marks = v.key_marks(p)
    got = "、".join(m["label"] for m in marks if m["hit"])
    miss = "、".join(m["label"] for m in marks if not m["hit"])
    return f"惜しい！（おさえた要点：{got}／まだの要点：{miss}）"


def verdict_text(v: judge.Verdict, p: dict | None = None) -> str:
    return {"correct": "正解！",
            "close": close_text(v, p) if p else f"惜しい！（要点 {v.hits}/{len(v.keys)} をおさえています）",
            "wrong": "違います",
            "scattershot": "候補を並べるのはナシです。答えは1つに絞ってください"}[v.level]


class AnswerCache:
    """同じ問題への同じ質問には同じ答え。ファイルに保存して、対戦・コマンドの間で共有する。"""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.data: dict[str, list[str]] = {}
        if path and path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8") or "{}")

    @staticmethod
    def _k(key: tuple[str, str]) -> str:
        return f"{key[0]}|{key[1]}"

    def get(self, key: tuple[str, str]) -> tuple[str, str] | None:
        v = self.data.get(self._k(key))
        return (v[0], v[1]) if v else None

    def put(self, key: tuple[str, str], value: tuple[str, str]) -> None:
        self.data[self._k(key)] = list(value)
        if not self.path:
            return
        # 並行して書く人がいても消し合わないよう、ロックして読み直してから足す
        with _locked(self.path) as f:
            f.seek(0)
            raw = f.read()
            current = json.loads(raw) if raw.strip() else {}
            current[self._k(key)] = list(value)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(current, ensure_ascii=False))
            self.data = current


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
