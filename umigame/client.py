"""Jev (TypeSafe AI) クライアントの用意。

- .env から TYPESAFE_API_KEY を読む（既存の環境変数は上書きしない）
- JEV_MOCK=1 のときは、ネットワークを使わないモックを返す（画面やロジックの確認用。判定の質は測れない）
"""

from __future__ import annotations

import hashlib
import inspect
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, Choice, RetryPolicy

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    """このプロジェクトの .env と、JEV_ENV_FILE（別プロジェクトとキーを共有するとき）の両方を読む。"""
    paths = [path] if path else [PROJECT_ROOT / ".env"] + (
        [Path(os.environ["JEV_ENV_FILE"])] if os.environ.get("JEV_ENV_FILE") else [])
    for env_path in paths:
        _read_env(env_path)


def _read_env(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def is_mock() -> bool:
    return os.environ.get("JEV_MOCK", "").strip().lower() in {"1", "true", "yes"}


def make_client(*, retries: int = 1, timeout: float = 10.0) -> Any:
    """AsyncTypeSafeClient か MockClient を返す。

    ゲームでは待たせるより早く失敗したほうがよいので、再試行は少なめ・タイムアウトは短め。
    """
    load_env()
    if is_mock():
        return MockClient()
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise SystemExit("TYPESAFE_API_KEY がありません。.env.example をコピーして .env を作るか、JEV_MOCK=1 で動かしてください。")
    return AsyncTypeSafeClient(
        api_key=api_key,
        model=os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest"),
        retry=RetryPolicy(max_retries=retries, backoff_initial=0.3, backoff_max=2.0),
        timeout=timeout,
    )


async def close_client(client: Any) -> None:
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - 終了処理の失敗で本来のエラーを隠さない
        pass


# --------------------------------------------------------------------------
# モック
# --------------------------------------------------------------------------
@dataclass
class _Choice:
    choice: str
    confidence: float
    probabilities: dict[str, float]
    type: str = "choice"


@dataclass
class _Noul:
    noul: float
    type: str = "noul"


@dataclass
class _Usage:
    input_tokens: int


@dataclass
class _Response:
    answers: dict[str, Any]
    usage: _Usage = field(default_factory=lambda: _Usage(0))


def _h(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32


class MockClient:
    """state と質問のハッシュで決まった答えを返す。同じ入力には同じ答え。"""

    async def system_one(self, state: Any, questions: dict[str, Any], **_: Any) -> _Response:
        s = repr(state)
        answers: dict[str, Any] = {}
        for key, q in questions.items():
            if isinstance(q, Choice):
                labels = list(q.criteria)
                raw = [_h(s, key, label) ** 3 for label in labels]
                total = sum(raw) or 1.0
                probs = {label: r / total for label, r in zip(labels, raw)}
                top = max(probs, key=probs.get)
                answers[key] = _Choice(top, probs[top], probs)
            elif key == "injection":
                answers[key] = _Noul(0.0)  # でたらめだと半分の入力が「指示あり」で止まるので、いつも「なし」
            else:
                answers[key] = _Noul(_h(s, key))
        return _Response(answers, _Usage(len(s) // 2))
