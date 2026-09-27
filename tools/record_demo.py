"""記事用のデモ（GIF）と画面写真を、例題「玄関の灯り」で撮る。本物の Jev を使う。

    # 先にデモ用のサーバーを 8790 番で起動しておく（UMIGAME_DEMO=1 で例題が出る）
    ../jev-lab/.venv/bin/python -m tools.record_demo --out docs/img              # ブラウザ幅（記事用）
    ../jev-lab/.venv/bin/python -m tools.record_demo --out docs/img/sp --mobile  # スマホ幅（SNS 用）

ゲームの問題は答えがわかってしまうので撮らない。例題は記事で答えを書いてよい問題。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from tools.capture import Browser
from tools.make_gif import make_gif

# デモの流れ。Jev が働いているところ（答えと確率・質問でない入力の見分け・解答の判定）を見せる
SCENARIOS = {
    "p001": {  # 3時の目覚まし（ゲームの★1。記事の例）
        "steps": [
            ("ask", "目覚まし時計は壊れていますか？"),
            ("ask", "男は夜に働いていますか？"),
            ("ask", "ヒントください"),                  # 質問ではない入力 → Jev が見分けて定型文
            ("guess", "男は夜勤だから"),                # 要点が1つ足りない → 惜しい ✓✗
            ("ask", "その3時は、午後3時ですか？"),
        ],
        "final": "男は夜勤で昼間に眠っていて、目覚ましの3時は午後3時のこと",
    },
    "classic1": {  # 本家ウミガメのスープ（記事の冒頭用）。答えが映らないよう、最初の数問だけで止める
        "steps": [
            ("ask", "スープに毒は入っていましたか？"),
            ("ask", "男は以前にもウミガメのスープを飲んだことがありますか？"),
            ("ask", "ヒントください"),
            ("ask", "店の人はうそをついていましたか？"),
        ],
        "final": None,
    },
    "porch": {  # 玄関の灯り（記事用の例題）
        "steps": [
            ("ask", "女は暗いのが怖いのですか？"),
            ("ask", "誰かほかの人のためですか？"),
            ("ask", "ヒントください"),
            ("guess", "誰かほかの人のために、灯りをつけている"),
            ("ask", "その人は朝早く来ますか？"),
            ("ask", "その人は新聞配達ですか？"),
        ],
        "final": "新聞配達の少年が、夜明け前の暗い玄関で転ばないようにするため",
    },
}


def wait_idle(b: Browser, timeout: float = 30) -> None:
    t0 = time.time()
    time.sleep(0.3)
    while time.time() - t0 < timeout:
        if not b.js("busy"):
            time.sleep(1.2)  # 裏の到達度の判定とアニメーションを待つ
            return
        time.sleep(0.2)
    raise TimeoutError("答えが返ってきません")


def bottom(b: Browser) -> None:
    b.js("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(0.4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("docs/img"))
    ap.add_argument("--puzzle", default="p001", choices=sorted(SCENARIOS))
    ap.add_argument("--base", default="http://127.0.0.1:8790")
    ap.add_argument("--mobile", action="store_true", help="スマホ幅（390x844）で撮る。既定はノートPCのブラウザ（1280x800）")
    a = ap.parse_args()
    # スマホ幅は2倍で撮って GIF で半分に縮める。ブラウザ幅は画面が広いので1倍で撮る（GIF を作る時間と大きさを抑える）
    width, height, scale = (390, 844, 2) if a.mobile else (1280, 800, 1)
    frames_dir = a.out / "frames"
    frames: list[tuple[Path, int]] = []
    n = 0

    def frame(b: Browser, ms: int, still: str | None = None, gif: bool = True) -> None:
        nonlocal n
        n += 1
        p = b.shot(frames_dir / f"{n:02d}.png")
        if gif:
            frames.append((p, ms))
        if still:
            (a.out / still).write_bytes(p.read_bytes())

    def send(b: Browser, mode: str, text: str) -> None:
        b.js(f"setMode({mode!r})")
        b.js(f"document.getElementById('q').value = {text!r}")
        bottom(b)
        frame(b, 800)
        b.js("document.getElementById('f').requestSubmit()")
        wait_idle(b)
        b.js("hideResult()")
        bottom(b)

    with Browser(width=width, height=height, scale=scale) as b:
        scenario = SCENARIOS[a.puzzle]
        b.goto(f"{a.base}/?p={a.puzzle}", wait=2)
        b.js("localStorage.clear(); location.reload()")
        time.sleep(2)
        frame(b, 2000, "01-start.png")
        stills = {1: "02-answer.png", 2: "03-meta.png", 3: "04-close.png"}
        for i, (mode, text) in enumerate(scenario["steps"]):
            send(b, mode, text)
            frame(b, 2200 if i in stills else 1500, stills.get(i))
        if scenario["final"] is None:          # 解答まではしない（冒頭用）
            make_gif(a.out / "demo.gif", frames, shrink_by=scale)
            return
        # 要点のチェックリストがそろったところ（★1〜2）
        b.js("document.getElementById('checklist').scrollIntoView({block:'center'})")
        time.sleep(0.4)
        frame(b, 1800, "05-checklist.png")
        # 解答モードにすると、候補が出る（記事の静止画だけ）
        b.js("setMode('guess')")
        time.sleep(0.5)
        bottom(b)
        frame(b, 0, "06-choices.png", gif=False)
        # 文章で解答 → Jev が要点ごとに判定して正解
        b.js(f"document.getElementById('q').value = {scenario['final']!r}")
        frame(b, 900)
        b.js("document.getElementById('f').requestSubmit()")
        wait_idle(b)
        time.sleep(0.8)
        # GIF は、会話の中に出る Jev の判定（正解）で終える。結果・シェアの画面は GIF に入れない
        b.js("hideResult()")
        bottom(b)
        frame(b, 3500, "07-correct.png")
        # 記事用の静止画：結果の画面と、Jev の中身（質問ごとの判定と確率）
        b.js("showResult(cur.id); document.getElementById('modal-card').scrollTop = 0")
        time.sleep(0.5)
        frame(b, 0, "08-result.png", gif=False)
        b.js("document.querySelector('#modal-card .jevbox details').open = true;"
             "document.querySelector('#modal-card .jevbox').scrollIntoView({block:'start'})")
        time.sleep(0.5)
        frame(b, 0, "09-jev.png", gif=False)
    make_gif(a.out / "demo.gif", frames, shrink_by=scale)


if __name__ == "__main__":
    main()
