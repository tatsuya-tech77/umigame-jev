"""ChatGPT のモデルを、Codex CLI（ChatGPT のアカウントでログイン）で AI プレイヤーにする。

    ../jev-lab/.venv/bin/python -m tools.codex_play --models gpt-6-sol --pids p001 --runs 1   # 試しに1ゲーム
    ../jev-lab/.venv/bin/python -m tools.codex_play --models gpt-6-astra gpt-6-sol gpt-6-luna --runs 3

Claude のときと同じく、1ゲームごとに新しいセッションで、play_cli を1手ずつ呼ばせる。
Codex は空の作業フォルダで動かし、使えるのは `./play`（play_cli を呼ぶだけのスクリプト）だけと伝える。
ほかのコマンド（ファイルを読むなど）を実行した回は、記録から外して「失格」と表示する。
結果は play_cli が results/arena-claude-code.jsonl に残す（"via" は claude-code のまま）。
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from umigame import puzzles
from umigame.client import PROJECT_ROOT

CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex"
PYTHON = PROJECT_ROOT.parent / "jev-lab" / ".venv" / "bin" / "python"
JEV_ENV = PROJECT_ROOT.parent / "jev-lab" / ".env"
RESULTS = PROJECT_ROOT / "results"
LOGS = RESULTS / "codex"

PROMPT = """あなたは水平思考クイズ「ウミガメのスープ」のプレイヤーです。AI の水平思考を測る対戦に参加しています。できるだけ少ない回数で真相を突き止めてください。

## 遊び方（このコマンドだけを使う）
./play start            … 問題文とルールが表示される（最初に1回）
./play ask "質問文"      … はい／いいえで答えられる質問を1つだけ
./play guess "真相"      … 真相を1〜2文で解答する
./play status           … 今の状態

## 厳守
- 使ってよいのは ./play だけ。**ファイルを読む・一覧を見る・検索する・ほかのコマンドを実行することは禁止**（問題の答えが書かれたファイルがあるため。実行すれば失格）。Web 検索も禁止。
- 1回のコマンドで質問は1つだけ。答えを見てから次の質問を考える。
- 成績 = 数えた質問の回数 + 外した解答×2。少ないほど良い。解答は3回まで。候補を並べる解答は不正解。
- ゲームが終わる（解けるか、解答を3回使う）まで続ける。

## 終わったら報告
- 解けたか、成績、数えた質問の数、解答の回数
- 決め手になった質問と、その考え方（2〜3行）
- 出題者の答えで、おかしいと感じたものがあれば挙げる
"""


def wrapper(model: str, pid: str, run: int) -> str:
    cmd = (f"cd {shlex.quote(str(PROJECT_ROOT))} && JEV_ENV_FILE={shlex.quote(str(JEV_ENV))} "
           f"exec {shlex.quote(str(PYTHON))} -m umigame.play_cli \"$1\" --player {model} --pid {pid} --run {run}")
    return f'#!/bin/bash\nif [ $# -ge 2 ]; then {cmd} "$2"; else {cmd}; fi\n'


def commands(events: list[dict]) -> list[str]:
    """Codex が実行したコマンドの一覧（イベントの形が変わっても拾えるよう、command を持つ項目を集める）。"""
    out = []
    for e in events:
        item = e.get("item") or e.get("msg") or {}
        cmd = item.get("command") if isinstance(item, dict) else None
        if cmd and e.get("type", "").endswith("started") is False:
            out.append(cmd if isinstance(cmd, str) else " ".join(cmd))
    return list(dict.fromkeys(out))


def allowed(cmd: str) -> bool:
    c = cmd.strip()
    for prefix in ("bash -lc ", "/bin/bash -lc ", "/bin/zsh -lc ", "zsh -lc "):
        if c.startswith(prefix):
            c = shlex.split(c)[-1].strip()
    return c.startswith("./play ") or c == "./play"


def play(model: str, pid: str, run: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="umigame-") as d:
        w = Path(d) / "play"
        w.write_text(wrapper(model, pid, run))
        w.chmod(0o755)
        args = [CODEX, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                "-m", model, "-C", d, "-s", "workspace-write", "--add-dir", str(RESULTS),
                "-c", "sandbox_workspace_write.network_access=true", PROMPT]
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired as e:  # Codex が止まることがある。この回だけ失敗にして、ほかは続ける
            out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            p = subprocess.CompletedProcess(args, 124, out, "30分たっても終わらなかった")
    events = []
    for line in p.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / f"{model}--{pid}--{run}.jsonl").write_text(p.stdout)
    cmds = commands(events)
    bad = [c for c in cmds if not allowed(c)]
    return {"model": model, "pid": pid, "run": run, "code": p.returncode, "commands": len(cmds), "bad": bad,
            "stderr": p.stderr[-500:] if p.returncode else ""}


def drop_record(model: str, pid: str, run: int) -> None:
    """失格の回を記録から外す。"""
    rec = RESULTS / "arena-claude-code.jsonl"
    if not rec.exists():
        return
    keep = [l for l in rec.read_text().splitlines()
            if not (l.strip() and (r := json.loads(l)) and r["model"] == model and r["pid"] == pid and r["run"] == run)]
    rec.write_text("\n".join(keep) + ("\n" if keep else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--pids", nargs="+", default=[p["id"] for p in puzzles.PUZZLES])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--first-run", type=int, default=1)
    ap.add_argument("--parallel", type=int, default=3)
    a = ap.parse_args()
    jobs = [(m, pid, r) for m in a.models for pid in a.pids for r in range(a.first_run, a.first_run + a.runs)]
    with ThreadPoolExecutor(a.parallel) as ex:
        for res in ex.map(lambda j: play(*j), jobs):
            if res["bad"]:
                drop_record(res["model"], res["pid"], res["run"])
            status = "失格: " + " / ".join(res["bad"])[:200] if res["bad"] else ("エラー " + res["stderr"] if res["code"] else "OK")
            print(f"{res['model']} {res['pid']} #{res['run']}: コマンド{res['commands']}回 {status}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
