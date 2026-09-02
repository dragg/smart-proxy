from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.notifier import TelegramNotifier


class _FakeResp:
    def __init__(self, status_code: int = 200, text: str = "ok") -> None:
        self.status_code = status_code
        self.text = text


class _CapturingClient:
    def __init__(self, *, status_code: int = 200, exc: Exception | None = None) -> None:
        self.status_code = status_code
        self.exc = exc
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, **kwargs) -> _FakeResp:
        self.posts.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return _FakeResp(self.status_code)


class TelegramNotifierTests(unittest.TestCase):
    def test_notify_posts_to_sendmessage_with_chat_and_text(self) -> None:
        async def run() -> None:
            client = _CapturingClient()
            n = TelegramNotifier("BOTTOKEN", "12345", client)
            ok = await n.notify("hello world")

            self.assertTrue(ok)
            self.assertEqual(len(client.posts), 1)
            url, kwargs = client.posts[0]
            self.assertEqual(url, "https://api.telegram.org/botBOTTOKEN/sendMessage")
            self.assertEqual(kwargs["json"]["chat_id"], "12345")
            self.assertEqual(kwargs["json"]["text"], "hello world")

        asyncio.run(run())

    def test_notify_returns_false_on_non_200_without_raising(self) -> None:
        async def run() -> None:
            client = _CapturingClient(status_code=403)
            n = TelegramNotifier("t", "c", client)
            self.assertFalse(await n.notify("x"))

        asyncio.run(run())

    def test_notify_swallows_transport_errors(self) -> None:
        async def run() -> None:
            client = _CapturingClient(exc=RuntimeError("boom"))
            n = TelegramNotifier("t", "c", client)
            # Must NOT propagate — a broken Telegram must never break the caller.
            self.assertFalse(await n.notify("x"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
