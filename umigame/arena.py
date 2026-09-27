"""AI 対戦: Jev が出題者、いろいろな LLM がプレイヤーになって、何問で解けるかを比べる。

    # OpenRouter のキー1つで各社のモデルを同じ条件で呼ぶ
    OPENROUTER_API_KEY=... .venv/bin/python -m umigame.arena --models openai/gpt-x google/gemini-x ...
    .venv/bin/python -m umigame.arena --models scripted      # API を使わない動作確認（Jev は本物）
    .venv/bin/python -m umigame.arena --list-models gpt      # OpenRouter のモデル ID を探す

- プレイヤーは問題文と、これまでの質問と答えだけを見る（真相は見ない）
- 1手ごとに JSON で「質問」か「解答」を返させる
- 同じ質問への Jev の答えはモデル間で共有する（確率の揺れで有利不利が出ないように）
- 結果は results/arena-<日時>.jsonl に1ゲーム1行で保存し、最後に表を出す
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import puzzles
from .client import PROJECT_ROOT, close_client, load_env, make_client
from .play_cli import RULES
from .session import MAX_GUESSES, MAX_QUESTIONS, AnswerCache, Session
MAX_TOKENS = 1500          # 1手の出力（思考を含む）の上限。思考トークンも出力として課金される
REASONING_EFFORT = "low"   # 考える深さは全モデルで揃える（対応していないモデルでは無視される）
EMPTY_RETRIES = 2          # 空・壊れた返答は数えずにやり直す回数
OPENROUTER_URL = "https://openrouter.ai/api/v1"
RATE_LIMIT_RETRIES = 6     # 429（回数制限）のときに待ってやり直す回数
RESULTS = PROJECT_ROOT / "results"

# Claude Code のサブエージェント（play_cli）と同じルール文を使う
SYSTEM_PROMPT = """あなたは水平思考クイズ「ウミガメのスープ」のプレイヤーです。
{rules}
真相がわかったら解答してください。解答は、なぜそうなったのかを1〜2文で具体的に書きます。

毎回、次のどちらか1つだけを JSON で返してください。ほかの文章は書かないでください。
{{"type": "question", "text": "質問（日本語、1つだけ）"}}
{{"type": "guess", "text": "真相（日本語、1〜2文）"}}"""


# --------------------------------------------------------------------------
# プレイヤー
# --------------------------------------------------------------------------
@dataclass
class Move:
    type: str          # question / guess
    text: str
    in_tokens: int = 0
    out_tokens: int = 0
    cost_usd: float = 0.0


def _transcript(p: dict, s: Session) -> str:
    lines = [f"問題（★{p['level']}）: {p['story']}", "", "これまでのやりとり:"]
    if not s.log:
        lines.append("（まだありません）")
    for t in s.log:
        if t["type"] == "question":
            mark = "" if t.get("counted", True) else "（数えない）"
            lines.append(f"Q: {t['text']} → {t['answer']}{mark}")
            if "meter" in t:
                lines.append(f"  到達度: {t['meter']}")
        else:
            lines.append(f"解答: {t['text']} → {t['answer']}")
    rest_q = MAX_QUESTIONS - s.questions
    lines += ["", f"残り: 質問 {rest_q} 回、解答 {MAX_GUESSES - s.guesses} 回。今の成績 {s.score}。"
              + ("質問はもうできないので解答してください。" if rest_q <= 0 else "次の一手を JSON で。")]
    return "\n".join(lines)


def parse_move(text: str) -> Move | None:
    """モデルの返答から JSON を取り出す。取れなければ None（やり直し）。"""
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if d.get("type") in ("question", "guess") and str(d.get("text", "")).strip():
                return Move(d["type"], str(d["text"]).strip()[:300])
        except json.JSONDecodeError:
            pass
    return None


class OpenRouterPlayer:
    def __init__(self, model: str, api_key: str, *, temperature: float | None = None):
        self.model, self.api_key, self.temperature = model, api_key, temperature

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(
            f"{OPENROUTER_URL}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                     "X-Title": "umigame-jev arena"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)

    async def move(self, p: dict, s: Session) -> Move:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(rules=RULES)},
                {"role": "user", "content": _transcript(p, s)},
            ],
            "usage": {"include": True},
            "max_tokens": MAX_TOKENS,
            "reasoning": {"effort": REASONING_EFFORT},
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        last: Exception | None = None
        spent = Move("question", "")
        attempt = limited = 0
        while attempt < 3 + EMPTY_RETRIES:
            try:
                d = await asyncio.to_thread(self._post, body)
            except urllib.error.HTTPError as e:
                if e.code == 402:
                    raise OutOfCredits(f"{self.model}: OpenRouter のクレジットが足りません（402）") from e
                if e.code == 429 and limited < RATE_LIMIT_RETRIES:
                    # 回数制限は数えずに待つ（Retry-After があれば従う）
                    limited += 1
                    wait = float(e.headers.get("Retry-After") or 0) or 5 * limited
                    await asyncio.sleep(min(wait, 60))
                    continue
                last = e
                attempt += 1
                await asyncio.sleep(2 * attempt)
                continue
            except Exception as e:  # noqa: BLE001 - 一時的な失敗は少し待って再試行
                last = e
                attempt += 1
                await asyncio.sleep(2 * attempt)
                continue
            attempt += 1
            u = d.get("usage") or {}
            # 失敗した返答の費用も数える
            spent.in_tokens += int(u.get("prompt_tokens") or 0)
            spent.out_tokens += int(u.get("completion_tokens") or 0)
            spent.cost_usd += float(u.get("cost") or 0.0)
            mv = parse_move((d.get("choices") or [{}])[0].get("message", {}).get("content") or "")
            if mv:
                mv.in_tokens, mv.out_tokens, mv.cost_usd = spent.in_tokens, spent.out_tokens, spent.cost_usd
                return mv
            last = RuntimeError("JSON で返ってこなかった")
        raise RuntimeError(f"{self.model}: {last}")


class OutOfCredits(RuntimeError):
    """OpenRouter のクレジット切れ（402）。続けても全部失敗するので、対戦全体を止める。"""


class ScriptedPlayer:
    """API を使わない動作確認用。確認用の質問を順に聞き、最後に用意した正解を解答する。"""

    model = "scripted"

    def __init__(self, p: dict):
        self.qs = [q for q, _ in p["gold"]][:8]
        self.guess = next(g for g, ok in p["guesses"] if ok)

    async def move(self, p: dict, s: Session) -> Move:
        asked = sum(t["type"] == "question" for t in s.log)
        if asked < len(self.qs):
            return Move("question", self.qs[asked])
        return Move("guess", self.guess)


# --------------------------------------------------------------------------
# 1ゲーム
# --------------------------------------------------------------------------
@dataclass
class Game:
    model: str
    pid: str
    level: int = 0
    solved: bool = False
    score: int = 0              # 数えた質問 + 外した解答 × 2
    questions: int = 0
    uncounted: int = 0
    guesses: int = 0
    wrong_guesses: int = 0
    player_in_tokens: int = 0
    player_out_tokens: int = 0
    player_cost_usd: float = 0.0
    jev_tokens: int = 0
    seconds: float = 0.0
    via: str = "openrouter"
    error: str = ""
    log: list[dict] = field(default_factory=list)


class Budget:
    """対戦全体のプレイヤー費用の上限。超えたら新しい手を打たずに終える。"""

    def __init__(self, max_usd: float):
        self.max_usd, self.spent = max_usd, 0.0

    @property
    def exceeded(self) -> bool:
        return self.spent >= self.max_usd


MAX_TURNS = 80  # 数えない入力を含めた手の数の上限（無限ループよけ）


async def play(jev: Any, player: Any, p: dict, cache: AnswerCache, budget: Budget | None = None) -> Game:
    g = Game(player.model, p["id"], p["level"])
    s = Session(pid=p["id"], player=player.model)
    t0 = time.perf_counter()
    try:
        for _ in range(MAX_TURNS):
            if s.finished:
                break
            if budget and budget.exceeded:
                g.error = "対戦全体の費用の上限に達したので中断"
                break
            mv = await player.move(p, s)
            g.player_in_tokens += mv.in_tokens
            g.player_out_tokens += mv.out_tokens
            g.player_cost_usd += mv.cost_usd
            if budget:
                budget.spent += mv.cost_usd
            if mv.type == "guess" or not s.can_ask:
                await s.guess(jev, p, mv.text)
            else:
                await s.ask(jev, p, mv.text[:120], cache)
    except OutOfCredits:
        raise
    except Exception as e:  # noqa: BLE001 - 1ゲームの失敗で全体を止めない
        g.error = str(e)[:300]
    g.solved, g.score, g.questions, g.uncounted = s.solved, s.score, s.questions, s.uncounted
    g.guesses, g.wrong_guesses, g.jev_tokens, g.log = s.guesses, s.wrong_guesses, s.jev_tokens, s.log
    g.seconds = time.perf_counter() - t0
    return g


# --------------------------------------------------------------------------
# 集計
# --------------------------------------------------------------------------
def summarize(games: list[Game]) -> str:
    rows = []
    for model in dict.fromkeys(g.model for g in games):
        gs = [g for g in games if g.model == model]
        solved = [g for g in gs if g.solved]
        avg_q = sum(g.score for g in solved) / len(solved) if solved else float("nan")
        cost = sum(g.player_cost_usd for g in gs)
        secs = sum(g.seconds for g in gs) / len(gs)
        errs = sum(bool(g.error) for g in gs)
        rows.append((len(solved) / len(gs), -avg_q if solved else -999, model,
                     f"| {model} | {len(solved)}/{len(gs)} | {avg_q:.1f} | "
                     f"{sum(g.guesses for g in gs) / len(gs):.1f} | ${cost:.3f} | {secs:.0f}s | {errs} |"))
    rows.sort(reverse=True)
    head = ("| モデル | 解けた | 解けたときの平均成績 | 平均解答回数 | プレイヤー費用 | 1ゲーム平均時間 | エラー |\n"
            "|---|---|---|---|---|---|---|")
    return "\n".join([head] + [r[-1] for r in rows])


def list_models(query: str) -> None:
    with urllib.request.urlopen(f"{OPENROUTER_URL}/models", timeout=30) as r:
        data = json.load(r)["data"]
    for m in sorted(data, key=lambda m: m.get("created", 0), reverse=True):
        if query.lower() in m["id"].lower():
            pr = m.get("pricing", {})
            print(f"{m['id']:<50} in ${float(pr.get('prompt', 0))*1e6:.2f}/M  out ${float(pr.get('completion', 0))*1e6:.2f}/M")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[])
    ap.add_argument("--puzzles", nargs="+", default=[p["id"] for p in puzzles.PUZZLES])
    ap.add_argument("--rounds", type=int, default=1, help="同じモデル×問題を何回やるか")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--max-usd", type=float, default=5.0, help="プレイヤー費用の合計の上限（ドル）")
    ap.add_argument("--list-models", metavar="QUERY")
    a = ap.parse_args()
    if a.list_models is not None:
        list_models(a.list_models)
        return

    load_env()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if any(m != "scripted" for m in a.models) and not key:
        sys.exit("OPENROUTER_API_KEY がありません（.env に書くか環境変数で渡してください）")

    jev = make_client()
    cache = AnswerCache(RESULTS / "answer_cache.json")
    budget = Budget(a.max_usd)
    sem = asyncio.Semaphore(a.parallel)
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"arena-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    async def one(model: str, pid: str) -> Game:
        p = puzzles.get(pid)
        player = ScriptedPlayer(p) if model == "scripted" else OpenRouterPlayer(model, key)
        async with sem:
            g = await play(jev, player, p, cache, budget)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(g), ensure_ascii=False) + "\n")
        mark = "✓" if g.solved else ("!" if g.error else "✗")
        print(f"  {mark} {model:<40} {pid:<9} 成績 {g.score:>2}（質問 {g.questions} 解答 {g.guesses}） "
              f"{g.seconds:>5.0f}s {g.error[:60]}", flush=True)
        return g

    try:
        jobs = [one(m, pid) for _ in range(a.rounds) for m in a.models for pid in a.puzzles]
        games = await asyncio.gather(*jobs)
    except OutOfCredits as e:
        sys.exit(f"\n中断: {e}\n途中までの記録: {out}（クレジットを足してから、この記録は消して回し直してください）")
    finally:
        await close_client(jev)
    print("\n" + summarize(games))
    print(f"\n記録: {out}")
    jev_tokens = sum(g.jev_tokens for g in games)
    print(f"Jev（出題者）の入力 {jev_tokens:,} トークン ≒ ${jev_tokens * 0.042 / 1e6:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
