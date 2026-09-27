"""記事用の画面写真を撮る。インストール済みの Google Chrome を裏で動かし、DevTools の仕組み（CDP）で操作する。

追加のライブラリは使わない（WebSocket も標準ライブラリだけで話す）。Chrome は使い捨てのプロフィールで起動するので、
普段使いの Chrome の履歴やログインには触れない。

    from tools.capture import Browser
    with Browser(width=390, height=844, scale=2) as b:
        b.goto("http://127.0.0.1:8790/?p=porch")
        b.shot("docs/img/start.png")
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class WebSocket:
    """CDP と話すだけの最小の WebSocket クライアント（テキストフレームのみ）。"""

    def __init__(self, url: str):
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError(head.decode(errors="replace"))

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            buf += chunk
        return buf

    def send(self, text: str) -> None:
        data = text.encode()
        head = bytes([0x81])
        n = len(data)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            op = b1 & 0x0F
            if op == 9:  # ping → pong は省略（CDP は送ってこない）
                continue
            if op == 8:
                raise ConnectionError("WebSocket closed by Chrome")
            parts.append(payload)
            if b1 & 0x80:
                return b"".join(parts).decode()


class Browser:
    def __init__(self, width: int = 390, height: int = 844, scale: float = 2, port: int = 9333):
        self.width, self.height, self.scale, self.port = width, height, scale, port
        self.profile = tempfile.mkdtemp(prefix="umigame-capture-")
        self.proc: subprocess.Popen | None = None
        self.ws: WebSocket | None = None
        self.next_id = 0

    def __enter__(self) -> "Browser":
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile}",
             "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--lang=ja-JP",
             f"--window-size={self.width},{self.height}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2))
                page = next(p for p in pages if p.get("type") == "page")
                break
            except Exception:  # noqa: BLE001 - 起動を待つ
                time.sleep(0.1)
        else:
            raise RuntimeError("Chrome が起動しませんでした")
        self.ws = WebSocket(page["webSocketDebuggerUrl"])
        self.call("Emulation.setDeviceMetricsOverride",
                  width=self.width, height=self.height, deviceScaleFactor=self.scale, mobile=True)
        self.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        return self

    def __exit__(self, *exc) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def call(self, method: str, **params):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expression: str):
        r = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"])
        return r.get("result", {}).get("value")

    def goto(self, url: str, wait: float = 1.5) -> None:
        self.call("Page.enable")
        self.call("Page.navigate", url=url)
        time.sleep(wait)

    def shot(self, path: str | Path, full: bool = False) -> Path:
        params = {"format": "png"}
        if full:
            h = self.js("Math.ceil(document.documentElement.scrollHeight)")
            params |= {"captureBeyondViewport": True,
                       "clip": {"x": 0, "y": 0, "width": self.width, "height": h, "scale": 1}}
        data = self.call("Page.captureScreenshot", **params)["data"]
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path
