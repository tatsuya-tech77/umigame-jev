"""画面写真（PNG）をつないで、動くGIFを作る。追加のライブラリは使わない。

    python3 tools/make_gif.py out.gif a.png:1500 b.png:800 c.png:2500 --shrink 2

- 各 PNG の後ろの数字は表示時間（ミリ秒）
- --shrink 2 で縦横を半分にする（高解像度で撮った写真を軽くする）
- 前のフレームから変わった部分だけを書くので、画面の一部だけ変わるデモは軽くなる
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from palette import read_png, shrink  # noqa: E402


def build_palette(frames: list[list[tuple[int, int, int]]], size: int = 256) -> list[tuple[int, int, int]]:
    """よく出る色から順に選ぶ（UI の画面は色数が少ないので、これで十分きれいに出る）。"""
    count: Counter = Counter()
    for px in frames:
        count.update(px)
    return [c for c, _ in count.most_common(size)]


def mapper(pal: list[tuple[int, int, int]]):
    exact = {c: i for i, c in enumerate(pal)}
    cache: dict = {}

    def index(c):
        i = exact.get(c)
        if i is not None:
            return i
        i = cache.get(c)
        if i is None:
            r, g, b = c
            i = min(range(len(pal)), key=lambda k: (pal[k][0] - r) ** 2 * 3 + (pal[k][1] - g) ** 2 * 4 + (pal[k][2] - b) ** 2 * 2)
            cache[c] = i
        return i
    return index


def lzw(data: list[int], min_size: int = 8) -> bytes:
    clear, eoi = 1 << min_size, (1 << min_size) + 1
    size, next_code = min_size + 1, eoi + 1
    table: dict[int, int] = {}
    out = bytearray()
    bits = nbits = 0

    def emit(code: int) -> None:
        nonlocal bits, nbits
        bits |= code << nbits
        nbits += size
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8

    emit(clear)
    prefix = data[0]
    for k in data[1:]:
        key = (prefix << 8) | k
        code = table.get(key)
        if code is not None:
            prefix = code
            continue
        emit(prefix)
        if next_code < 4096:
            table[key] = next_code
            next_code += 1
            if next_code > (1 << size) and size < 12:
                size += 1
        else:
            emit(clear)
            table.clear()
            size, next_code = min_size + 1, eoi + 1
        prefix = k
    emit(prefix)
    emit(eoi)
    if nbits:
        out.append(bits & 0xFF)
    return bytes(out)


def sub_blocks(data: bytes) -> bytes:
    return b"".join(bytes([len(data[i:i + 255])]) + data[i:i + 255] for i in range(0, len(data), 255)) + b"\x00"


def make_gif(out: Path, items: list[tuple[Path, int]], shrink_by: int = 1) -> None:
    frames, w, h = [], 0, 0
    for path, _ in items:
        fw, fh, px = read_png(path)
        if shrink_by > 1:
            fw, fh, px = shrink(fw, fh, px, fw // shrink_by)
        if w and (fw, fh) != (w, h):
            sys.exit(f"{path}: 大きさがそろっていません（{fw}x{fh}、最初は {w}x{h}）")
        w, h = fw, fh
        frames.append(px)
    pal = build_palette(frames)
    idx = mapper(pal)
    table = pal + [(0, 0, 0)] * (256 - len(pal))

    gif = bytearray(b"GIF89a" + struct.pack("<HHBBB", w, h, 0xF7, 0, 0))
    gif += b"".join(bytes(c) for c in table)
    gif += b"\x21\xFF\x0BNETSCAPE2.0\x03\x01\x00\x00\x00"  # くり返し再生
    prev: list[int] | None = None
    for (path, delay), px in zip(items, frames):
        cur = [idx(c) for c in px]
        # 前のフレームと違う部分だけを切り出す
        x0, y0, x1, y1 = 0, 0, w - 1, h - 1
        if prev is not None:
            rows = [y for y in range(h) if cur[y * w:(y + 1) * w] != prev[y * w:(y + 1) * w]]
            if not rows:
                rows = [0]
            y0, y1 = rows[0], rows[-1]
            cols = [x for x in range(w) if any(cur[y * w + x] != prev[y * w + x] for y in range(y0, y1 + 1))] or [0]
            x0, x1 = cols[0], cols[-1]
        rect = [cur[y * w + x] for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        gif += b"\x21\xF9\x04" + struct.pack("<BHBB", 0x04, max(2, round(delay / 10)), 0, 0)
        gif += b"\x2C" + struct.pack("<HHHHB", x0, y0, x1 - x0 + 1, y1 - y0 + 1, 0)
        gif += b"\x08" + sub_blocks(lzw(rect))
        prev = cur
    gif += b"\x3B"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(gif)
    print(f"{out}: {w}x{h}, {len(items)}コマ, {len(gif):,} バイト")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("frames", nargs="+", help="画像.png:表示ミリ秒")
    ap.add_argument("--shrink", type=int, default=1)
    a = ap.parse_args()
    items = []
    for f in a.frames:
        path, _, ms = f.rpartition(":")
        items.append((Path(path), int(ms)))
    make_gif(a.out, items, a.shrink)


if __name__ == "__main__":
    main()
