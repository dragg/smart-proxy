"""Minimal outbound notification for operational alerts (Telegram).

The notifier is strictly fire-and-forget: ``notify`` never raises and never blocks
the caller beyond its own short timeout, so a broken or slow Telegram can never break
the OAuth refresh path that emits the alert."""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

_ALERT_THROTTLE_WINDOW_S = 30 * 60
_ALERT_THROTTLE_MAX_SIGNATURES = 512


class AlertThrottle:
    """Collapse a storm of identical failures into one message per window.

    A bug on the request path fires once per request -- the session-ranking
    overflow answered 500 on every dashboard poll -- so alerting each occurrence
    would bury the channel and train everyone to ignore it. The first occurrence
    of a signature goes out immediately; repeats inside ``window_seconds`` are
    counted instead of sent, and the next send past the window carries that
    count so nothing is silently dropped.

    Windows and pending repeat counts live in memory only: a restart can cost at
    most one duplicate alert per signature, which is the right trade against
    persisting alerting state.
    """

    def __init__(
        self,
        window_seconds: float = _ALERT_THROTTLE_WINDOW_S,
        max_signatures: int = _ALERT_THROTTLE_MAX_SIGNATURES,
    ) -> None:
        self._window = window_seconds
        self._max_signatures = max_signatures
        # signature -> [last_sent_at, suppressed_since_then]
        self._seen: dict[str, list[float]] = {}

    def should_send(self, signature: str, *, now: float | None = None) -> int | None:
        """Return the suppressed-repeat count to report, or None to stay silent.

        0 means "send, nothing was suppressed"; N > 0 means "send, and mention
        that N were swallowed since the last message".
        """
        moment = time.monotonic() if now is None else now
        entry = self._seen.get(signature)
        if entry is None:
            self._evict_if_needed()
            self._seen[signature] = [moment, 0]
            return 0
        last_sent_at, suppressed = entry
        if moment - last_sent_at < self._window:
            entry[1] = suppressed + 1
            return None
        self._seen[signature] = [moment, 0]
        return int(suppressed)

    def _evict_if_needed(self) -> None:
        """Bound the table so an unbounded spread of signatures can't leak.

        Distinct signatures are finite in practice (exception type x source
        line), but a message baked into a signature by a future caller would
        make it unbounded -- drop the least recently sent first.
        """
        overflow = len(self._seen) + 1 - self._max_signatures
        if overflow <= 0:
            return
        stalest = sorted(self._seen, key=lambda sig: self._seen[sig][0])[:overflow]
        for signature in stalest:
            del self._seen[signature]


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._client = client

    async def notify(self, text: str) -> bool:
        """POST one message to Telegram. Returns True on success, False on any failure
        (never raises)."""
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        try:
            r = await self._client.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=_TELEGRAM_TIMEOUT,
            )
            if r.status_code != 200:
                logger.warning("Telegram notify HTTP %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:  # network/timeout/anything — must not propagate
            logger.warning("Telegram notify failed: %s", exc)
            return False
