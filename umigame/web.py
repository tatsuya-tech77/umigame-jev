"""ウミガメのスープ × Jev のブラウザ版。

    .venv/bin/python -m umigame.web              # http://127.0.0.1:8789/
    JEV_MOCK=1 .venv/bin/python -m umigame.web   # API を使わずに画面だけ確認

ゲームの進行（質問の履歴）はブラウザが持ち、毎回送ってくる。サーバーが持つのは
「同じ質問への答えのキャッシュ」「IP ごとの回数制限」（メモリ上）と、
「1日の利用料」（日本時間で日ごと。再起動しても消えないよう .budget.json に保存）だけ。
1日の利用料が上限に達したら、その日は店じまい（Jev を呼ばない。キャッシュ済みの答えとギブアップは使える）。
真相の文章はブラウザに送らない（正解するかギブアップしたときだけ返す）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import judge, puzzles, session
from .client import PROJECT_ROOT, close_client, is_mock, make_client

INDEX = Path(__file__).with_name("static") / "index.html"
BUDGET_FILE = Path(os.environ.get("UMIGAME_BUDGET_FILE") or Path(__file__).resolve().parents[1] / ".budget.json")
PRICE_PER_1M_INPUT_USD = 0.042
YEN_PER_USD = 150
JST = ZoneInfo("Asia/Tokyo")

MAX_QUESTION = 120
MAX_GUESS = 300
MAX_HISTORY = 100
# 1つの IP から1分間に受け付けるリクエスト数（質問1回で、答え＋裏の判定の2回になることがある）
PER_IP_PER_MIN = int(os.environ.get("UMIGAME_PER_IP_PER_MIN", "60"))
# 手前にいる中継サーバー（Render・Cloud Run などのロードバランサー）の数。0 なら接続元の IP をそのまま使う。
# 1 以上のときは X-Forwarded-For の右から数えてその位置の IP を使う（左側は利用者が偽れるので信じない）
TRUST_PROXY_HOPS = int(os.environ.get("UMIGAME_TRUST_PROXY_HOPS", "0"))
DEBUG_XFF = os.environ.get("UMIGAME_DEBUG_XFF") == "1"
# 1日の利用料の上限（全員の合計、円）。100円 ≒ 1,600万トークン ≒ 質問8,000回
DAILY_BUDGET_YEN = float(os.environ.get("UMIGAME_DAILY_BUDGET_YEN", "100"))
CACHE_MAX = 20000
# 1 なら、まだ測っていない AI の「仮の」成績を表に足す（画面の見た目を確かめるため。勝ち負けには使わない）
SAMPLE_AI = os.environ.get("UMIGAME_SAMPLE_AI") == "1"
# 仮の成績を出す AI（OpenRouter で名前を確かめたもの）と、Opus の手数に対するおおよその倍率
# 今の対戦相手（Claude Code で動かす Claude と、Codex で動かす ChatGPT）のうち、まだ測っていないものの仮の成績
SAMPLE_MODELS = [("GPT-6 Astra", 0.9), ("GPT-6 Sol", 1.1), ("GPT-6 Luna", 1.8)]

state: dict = {}


def yen(tokens: int) -> float:
    return tokens * PRICE_PER_1M_INPUT_USD / 1e6 * YEN_PER_USD


CLOSED = {"code": "closed", "message": "また明日（日本時間0時〜）遊びに来てください"}


class Guard:
    """IP ごとの回数制限と、1日の利用料の上限。プロセス1つで動かす前提。

    上限の判定は呼ぶ前に行うので、同時に来たリクエストの分だけ少しはみ出すことがある。
    """

    def __init__(self, budget_yen: float = DAILY_BUDGET_YEN, path: Path | None = BUDGET_FILE,
                 today=lambda: datetime.now(JST).date().isoformat()) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.swept = time.monotonic()
        self.budget_yen = budget_yen
        self.path = path
        self.today = today
        self.day, self.tokens = today(), 0
        if path and path.exists():
            try:
                saved = json.loads(path.read_text())
                if saved.get("day") == self.day:
                    self.tokens = int(saved.get("tokens", 0))
            except (ValueError, OSError):
                pass

    def _roll(self) -> None:
        if self.today() != self.day:
            self.day, self.tokens = self.today(), 0

    @property
    def spent_yen(self) -> float:
        self._roll()
        return yen(self.tokens)

    @property
    def is_open(self) -> bool:
        return self.spent_yen < self.budget_yen

    def check(self, ip: str) -> None:
        if not self.is_open:
            raise HTTPException(503, CLOSED)
        now = time.monotonic()
        q = self.hits[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= PER_IP_PER_MIN:
            raise HTTPException(429, "短い時間に送りすぎです。1分ほど待ってから送ってください")
        q.append(now)
        self._sweep(now)

    def _sweep(self, now: float) -> None:
        """1分以上来ていない IP の記録を消す（ずっと残してメモリが増えないように）。"""
        if now - self.swept < 60:
            return
        self.swept = now
        for ip in [ip for ip, q in self.hits.items() if not q or now - q[-1] > 60]:
            del self.hits[ip]

    def spend(self, tokens: int) -> None:
        self._roll()
        self.tokens += tokens
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"day": self.day, "tokens": self.tokens}))
            except OSError as e:
                # 書けなくても遊べるようにする。ただし再起動で今日の利用料が0に戻るので、ログで気づけるようにする
                print(f"利用料の記録を保存できません（{self.path}）: {e}", flush=True)

    def status(self) -> dict:
        return {"open": self.is_open, "used": min(1.0, self.spent_yen / self.budget_yen) if self.budget_yen else 1.0}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 接続を使い回すと1回あたりの待ち時間が短くなる
    state["client"] = make_client()
    state["guard"] = Guard()
    state["cache"] = {}
    try:
        yield
    finally:
        await close_client(state["client"])


app = FastAPI(lifespan=lifespan)
# 問題ごとの画像など（img/p001.webp …）。真相が描かれていない画像だけを置くこと
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")

Label = Literal["yes", "no", "irrelevant", "partly", "unsure"]
Question = Field(min_length=1, max_length=MAX_QUESTION)


class Turn(BaseModel):
    q: str = Question
    a: Label


class Ask(BaseModel):
    pid: str
    question: str = Question
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)


class History(BaseModel):
    pid: str
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)


class HintReq(BaseModel):
    pid: str
    history: list[Turn] = Field(default_factory=list, max_length=MAX_HISTORY)
    used: list[tuple[int, int]] = Field(default_factory=list, max_length=20)


class Guess(BaseModel):
    pid: str
    guess: str = Field(min_length=1, max_length=MAX_GUESS)


class Pid(BaseModel):
    pid: str


class Choose(BaseModel):
    pid: str
    id: str = Field(min_length=1, max_length=20)


def _puzzle(pid: str) -> dict:
    try:
        return puzzles.get(pid)
    except KeyError:
        raise HTTPException(404, "その問題はありません") from None


def client_ip(request: Request, hops: int | None = None) -> str:
    """回数制限に使う利用者の IP。中継サーバーの後ろでは X-Forwarded-For から取る。"""
    hops = TRUST_PROXY_HOPS if hops is None else hops
    direct = request.client.host if request.client else "-"
    if hops <= 0:
        return direct
    chain = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
    if DEBUG_XFF:  # 新しいホスティングで、X-Forwarded-For の並び方を確かめるときだけ
        print(f"x-forwarded-for={chain} direct={direct}", flush=True)
    # 中継サーバーは受け取った接続元を右端に足していく。右から hops 番目が、信じてよい一番外側
    return chain[-hops] if len(chain) >= hops else direct


def _pairs(history: list[Turn]) -> list[tuple[str, str]]:
    return [(t.q, t.a) for t in history]


async def _call(request: Request, coro_fn):
    """回数制限 → Jev 呼び出し → 予算の記録。Jev の失敗は 503 にして画面に伝える。"""
    guard: Guard = state["guard"]
    guard.check(client_ip(request))
    try:
        result = await coro_fn()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - 中身は出さず、再試行を促す
        raise HTTPException(503, "出題者が考えこんでいます。もう一度送ってください") from None
    guard.spend(result.input_tokens)
    return result


MODEL_NAMES = {
    "claude-opus-5.5": "Claude Opus 5.5",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-haiku-4.5": "Claude Haiku 4.5",
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6-sol": "GPT-6 Sol",
    "gpt-6-luna": "GPT-6 Luna",
}


def ai_results() -> dict[str, list[dict]]:
    """results/arena-*.jsonl から、問題ごと・AI ごとの成績（解けなかった回は無限として中央値）。

    AI ごとに、各回の手数と、中央値の回の答えの並び（はい／いいえ…のラベルだけ。質問の文は出さない）も返す。
    """
    runs: dict[tuple[str, str], list[dict]] = {}
    for f in sorted(glob.glob(str(PROJECT_ROOT / "results" / "arena-*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("model") == "scripted" or r.get("error"):
                continue
            runs.setdefault((r["pid"], r["model"]), []).append(r)
    out: dict[str, list[dict]] = {}
    for (pid, model), rs in runs.items():
        # 今の上限（質問の回数）を超えて解いた回は、解けなかった回として数える（上限を下げたときの古い記録）
        scores = [r.get("score", r.get("questions", 0))
                  if r.get("solved") and r.get("questions", 0) <= session.MAX_QUESTIONS else float("inf") for r in rs]
        med = statistics.median(scores)
        # 中央値に一番近い回を「代表の回」にする（3回なら真ん中の回そのもの）
        rep = rs[min(range(len(rs)), key=lambda i: (abs(scores[i] - med) if med != float("inf") else 0, i))]
        log = rep.get("log") or []
        out.setdefault(pid, []).append({
            "model": MODEL_NAMES.get(model, model),
            "score": None if med == float("inf") else med,
            "runs": len(scores), "solved": sum(x != float("inf") for x in scores),
            "scores": [None if x == float("inf") else x for x in scores],
            "path": [t["label"] for t in log if t.get("type") == "question" and t.get("counted", True)],
            "questions": rep.get("questions", 0), "wrong": rep.get("wrong_guesses", 0),
        })
    if SAMPLE_AI:
        for p in puzzles.PUZZLES:
            out.setdefault(p["id"], []).extend(sample_ai(p, out.get(p["id"], [])))
    for rows in out.values():  # 手数の少ない順（解けなかった AI は最後。その中では解けた回の多い順）
        rows.sort(key=lambda r: (r["score"] is None, r["score"] or 0, -r["solved"], r["model"]))
    return out


def sample_ai(p: dict, real: list[dict]) -> list[dict]:
    """まだ測っていない AI の仮の成績。問題と AI の名前から決まる（毎回同じ値）。"""
    import hashlib
    opus = next((r["score"] for r in real if r["model"] == "Claude Opus 5.5" and r["score"] is not None), None)
    base = opus if opus is not None else 3 * p.get("level", 3)
    rows = []
    measured = {r["model"] for r in real}
    for name, factor in SAMPLE_MODELS:
        if name in measured:
            continue
        seed = hashlib.sha256(f"{p['id']}|{name}".encode()).digest()
        scores = []
        for i in range(3):
            x = round(max(1, base * factor * (0.7 + seed[i] / 255 * 0.6)))
            scores.append(None if x > session.MAX_QUESTIONS + 2 * session.MAX_GUESSES else x)
        solved = [x for x in scores if x is not None]
        med = sorted(x if x is not None else 10 ** 6 for x in scores)[1]
        rows.append({"model": name, "score": None if med >= 10 ** 6 else med, "runs": 3,
                     "solved": len(solved), "scores": scores, "path": [], "questions": 0, "wrong": 0,
                     "sample": True})
    return rows


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


@app.get("/api/puzzles")
async def list_puzzles() -> dict:
    ai = ai_results()
    return {
        "mock": is_mock(),
        "price_per_1m": PRICE_PER_1M_INPUT_USD,
        # 回数の上限は AI 対戦（session.py）と同じ値を使う
        "rules": {"max_questions": session.MAX_QUESTIONS, "max_uncounted": session.MAX_UNCOUNTED,
                  "max_guesses": session.MAX_GUESSES},
        "shop": state["guard"].status() | ({} if state["guard"].is_open else {"message": CLOSED["message"]}),
        "puzzles": [{"id": p["id"], "title": p["title"], "story": p["story"],
                     "keys": len(p["keys"]), "level": p.get("level", 3),
                     "suggest": puzzles.suggestions(p), "ai": ai.get(p["id"], []),
                     # ★1〜2で解答の候補がある問題だけ、要点の名前を見せる（候補が出る条件のチェックリスト）
                     "key_labels": p.get("key_labels") if p.get("level", 3) <= 2 and p.get("choices") else None,
                     } for p in puzzles.PUZZLES],
    }


@app.post("/api/ask")
async def ask(req: Ask, request: Request) -> dict:
    p = _puzzle(req.pid)
    question = req.question.strip()
    key = judge.cache_key(p["id"], question)
    cache: dict = state["cache"]
    if key and key in cache:
        # 同じ質問には同じ答え（Jev の確率は毎回少し揺れるので、最初の答えを固定する）
        return cache[key] | {"cached": True, "ms": 0, "tokens": 0}
    r = await _call(request, lambda: judge.answer(state["client"], p, question, _pairs(req.history)))
    out = {"label": r.label, "text": r.text, "probabilities": r.probabilities,
           "ms": round(r.ms), "tokens": r.input_tokens, "cached": False}
    if key and len(cache) < CACHE_MAX:
        cache[key] = out
    return out


@app.post("/api/progress")
async def progress(req: History, request: Request) -> dict:
    p = _puzzle(req.pid)
    pr = await _call(request, lambda: judge.progress(state["client"], p, _pairs(req.history)))
    # 要点の文章は答えのネタバレになるので、数値だけ返す
    return {"keys": list(pr.keys.values()), "ratio": pr.ratio, "band": pr.band, "tokens": pr.input_tokens}


@app.post("/api/hint")
async def hint(req: HintReq, request: Request) -> dict:
    """到達度メーターで一番たどり着いていない要点を探し、その要点の手書きヒントを返す。"""
    p = _puzzle(req.pid)
    pr = await _call(request, lambda: judge.progress(state["client"], p, _pairs(req.history)))
    probs = list(pr.keys.values())
    picked = judge.pick_hint(p, probs, set(req.used))
    out = {"keys": probs, "ratio": pr.ratio, "tokens": pr.input_tokens}
    if picked is None:
        return out | {"hint": None, "text": "ヒントはもう全部出しました。解答に挑戦してみましょう"}
    i, level = picked
    return out | {"hint": [i, level], "text": p["hints"][i][level], "strong": level > 0}


@app.post("/api/guess")
async def guess(req: Guess, request: Request) -> dict:
    p = _puzzle(req.pid)
    v = await _call(request, lambda: judge.judge_guess(state["client"], p, req.guess.strip()))
    # 要点ごとの確率は返さない（少しずつ変えて当てにいくのを難しくする）
    out = {"level": v.level, "hits": v.hits, "total": len(v.keys), "tokens": v.input_tokens}
    if v.level == "close":
        # 惜しいときは、要点の名前ごとの当たり外れを返す（確率や要点の文は返さない）
        out["marks"] = v.key_marks(p)
    if v.level == "correct":
        out["truth"] = p["truth"]
    return out


@app.post("/api/choices")
async def choices(req: Pid, request: Request) -> dict:
    """解答の候補（★1〜2）。要点がそろったかはブラウザが判定する（自己申告）。"""
    state["guard"].check(client_ip(request))
    return {"choices": puzzles.choice_list(_puzzle(req.pid))}


@app.post("/api/choose")
async def choose(req: Choose, request: Request) -> dict:
    """候補を選んで解答する。Jev は使わない（正解は作者が決めてある）。"""
    state["guard"].check(client_ip(request))
    p = _puzzle(req.pid)
    if req.id not in {c["id"] for c in puzzles.choice_list(p)}:
        raise HTTPException(404, "その候補はありません")
    if puzzles.is_correct_choice(p, req.id):
        return {"level": "correct", "truth": p["truth"]}
    return {"level": "wrong"}


@app.post("/api/reveal")
async def reveal(req: Pid) -> dict:
    return {"truth": _puzzle(req.pid)["truth"]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    # ホスティングでは HOST=0.0.0.0 と PORT が環境変数で渡される
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8789")))
    a = ap.parse_args()
    print(f"ウミガメのスープ × Jev → http://{a.host}:{a.port}/" + ("  (MOCK)" if is_mock() else ""))
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
