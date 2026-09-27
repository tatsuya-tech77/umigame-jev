"""1日の利用料の上限（店じまい）と回数制限のテスト。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException, Request

from umigame import web

# 1円ぶんのトークン数
ONE_YEN = int(1 / web.yen(1_000_000) * 1_000_000)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "budget.json"
        self.day = "2026-09-25"

    def guard(self, budget=1.0):
        return web.Guard(budget_yen=budget, path=self.tmp, today=lambda: self.day)

    def test_closes_when_budget_is_used(self):
        g = self.guard()
        g.check("a")
        g.spend(ONE_YEN + 1)
        with self.assertRaises(HTTPException) as cm:
            g.check("a")
        self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(cm.exception.detail["code"], "closed")

    def test_reopens_next_day(self):
        g = self.guard()
        g.spend(ONE_YEN + 1)
        self.assertFalse(g.is_open)
        self.day = "2026-09-26"
        self.assertTrue(g.is_open)

    def test_survives_restart_on_same_day_only(self):
        self.guard().spend(ONE_YEN + 1)
        self.assertFalse(self.guard().is_open)
        self.day = "2026-09-26"
        self.assertTrue(self.guard().is_open)

    def test_rate_limit_per_ip(self):
        g = self.guard(budget=100)
        for _ in range(web.PER_IP_PER_MIN):
            g.check("a")
        with self.assertRaises(HTTPException) as cm:
            g.check("a")
        self.assertEqual(cm.exception.status_code, 429)
        g.check("b")  # 別の IP は影響を受けない

    def test_old_ips_are_swept(self):
        g = self.guard(budget=100)
        g.check("a")
        g.hits["a"][-1] -= 120        # 2分前に来たことにする
        g.swept -= 120
        g.check("b")
        self.assertNotIn("a", g.hits)
        self.assertIn("b", g.hits)


class ClientIpTest(unittest.TestCase):
    def req(self, xff=None, host="10.0.0.1"):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": (host, 1234)})

    def test_direct_by_default(self):
        self.assertEqual(web.client_ip(self.req("1.2.3.4"), hops=0), "10.0.0.1")

    def test_one_proxy_uses_rightmost(self):
        # 利用者が左側に偽の IP を入れても、中継サーバーが右端に足した本当の IP を使う
        self.assertEqual(web.client_ip(self.req("6.6.6.6, 1.2.3.4"), hops=1), "1.2.3.4")

    def test_missing_header_falls_back(self):
        self.assertEqual(web.client_ip(self.req(None), hops=1), "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
