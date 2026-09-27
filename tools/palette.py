"""画像を、ゲーム画面になじむ白黒4階調のドット絵に変換する。追加のライブラリは使わない（Python だけで PNG を読み書き）。

    python3 tools/palette.py 入力.png umigame/static/img/p001.png --width 240

- 入力は 8bit の PNG（RGB / RGBA、インターレースなし。ChatGPT の画像はこの形）
- 横幅 --width ピクセルまで縮めてドットを粗くし、各点を明るさで4段階に分けて置き換える
- 出力は4色のパレット PNG（とても軽い）。表示するときは CSS の image-rendering: pixelated で拡大する
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

# 白黒ベースの4階調（明るい順）。生成りと墨色はゲーム画面の背景・文字と同じ色
PALETTE = [
    (0xF6, 0xF4, 0xEF),  # 生成り（背景）
    (0xCF, 0xC8, 0xB8),  # 薄い灰
    (0x6B, 0x65, 0x58),  # 濃い灰（灰茶）
    (0x1D, 0x1B, 0x16),  # 墨色
]


def read_png(path: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit(f"{path}: PNG ではありません")
    pos, idat, width = 8, b"", 0
    while pos < len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color not in (2, 6) or interlace:
                sys.exit(f"{path}: 8bit の RGB / RGBA（インターレースなし）だけに対応しています")
            channels = 3 if color == 2 else 4
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = width * channels
    prev = bytearray(stride)
    pixels: list[tuple[int, int, int]] = []
    i = 0
    for _ in range(height):
        ftype = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + b) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        pixels.extend(tuple(line[x:x + 3]) for x in range(0, stride, channels))
        prev = line
    return width, height, pixels


def shrink(width: int, height: int, pixels, new_w: int):
    """ブロックごとの平均色で縮める（ドットを粗くする）。"""
    new_h = max(1, round(height * new_w / width))
    out = []
    for ny in range(new_h):
        y0, y1 = ny * height // new_h, max(ny * height // new_h + 1, (ny + 1) * height // new_h)
        for nx in range(new_w):
            x0, x1 = nx * width // new_w, max(nx * width // new_w + 1, (nx + 1) * width // new_w)
            r = g = b = n = 0
            for y in range(y0, y1):
                row = y * width
                for x in range(x0, x1):
                    pr, pg, pb = pixels[row + x]
                    r += pr; g += pg; b += pb; n += 1
            out.append((r // n, g // n, b // n))
    return new_w, new_h, out


def luma(rgb) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def nearest(rgb) -> int:
    """明るさが一番近い階調を選ぶ（色は捨てて白黒にする）。"""
    y = luma(rgb)
    return min(range(len(PALETTE)), key=lambda i: abs(y - luma(PALETTE[i])))


def write_png(path: Path, width: int, height: int, idx: list[int]) -> None:
    def chunk(t: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + t + body + struct.pack(">I", zlib.crc32(t + body) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(idx[y * width:(y + 1) * width])
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
    png += chunk(b"PLTE", b"".join(bytes(c) for c in PALETTE))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--width", type=int, default=240, help="縮めたあとの横幅（ドットの細かさ）")
    a = ap.parse_args()
    w, h, px = read_png(a.src)
    w, h, px = shrink(w, h, px, a.width)
    write_png(a.dst, w, h, [nearest(p) for p in px])
    print(f"{a.dst}: {w}x{h}, 4色, {a.dst.stat().st_size:,} バイト")


if __name__ == "__main__":
    main()
