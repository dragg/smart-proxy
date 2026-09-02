"""Per-proxy-key spend limits.

Holds the configured limits and the live per-key spend for the current window
in memory, so the proxy's request-path check is a dict lookup with no I/O. The
database is the recovery source: :meth:`KeyLimiter.load` re-reads limits and
prices and re-seeds spend from ``usage_key_hourly`` at startup and on reload.

The counter is exact for the lifetime of the process; a restart re-seeds from
the hourly buckets and therefore loses at most the <60s the previous process
had not yet flushed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo

from smart_proxy.usage import build_price_lookup, calculate_cost

logger = logging.getLogger(__name__)

HOUR_FMT = "%Y-%m-%dT%H"
DEFAULT_WINDOW_TZ = "Europe/Paris"


@dataclass(frozen=True)
class LimitKind:
    """A configurable limit type. ``label`` appears in the 429 message."""

    id: str
    window_hours: int
    label: str


# Every kind here currently shares one window (local midnight → local midnight),
# which is why spend is a single scalar per key. A future kind with a *different*
# window needs `_spent` keyed by kind and a per-kind rollover check.
LIMIT_KINDS: dict[str, LimitKind] = {
    "daily_usd": LimitKind(id="daily_usd", window_hours=24, label="24h"),
}


@dataclass(frozen=True)
class LimitBlock:
    """A key that has reached one of its limits."""

    kind: str
    label: str
    retry_after: int
    limit_usd: float
    spent_usd: float


class KeyLimiter:
    def __init__(self, db, *, tz: str = DEFAULT_WINDOW_TZ) -> None:
        self._db = db
        self._tz = ZoneInfo(tz)
        self._limits: dict[str, dict[str, float]] = {}
        self._spent: dict[str, float] = {}
        self._prices: dict | None = None
        self._window_start: datetime | None = None
        if self.window_start().minute != 0:
            logger.warning(
                "Limit window timezone %r has a non-whole-hour UTC offset; "
                "the daily window will not align with hourly usage buckets.",
                tz,
            )

    # -- window math ----------------------------------------------------

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def window_start(self, now: datetime | None = None) -> datetime:
        """Most recent local midnight, as a UTC instant."""
        current = now or self._now()
        local = current.astimezone(self._tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(timezone.utc)

    def window_end(self, now: datetime | None = None) -> datetime:
        """Next local midnight, as a UTC instant.

        The ``+ timedelta(days=1)`` is wall-clock arithmetic on a zone-aware
        datetime, so a DST day correctly yields a 23h or 25h window.
        """
        current = now or self._now()
        local = current.astimezone(self._tz)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return (midnight + timedelta(days=1)).astimezone(timezone.utc)

    def retry_after(self, now: datetime | None = None) -> int:
        current = now or self._now()
        return max(1, ceil((self.window_end(current) - current).total_seconds()))

    def _roll_if_needed(self, now: datetime | None = None) -> None:
        start = self.window_start(now or self._now())
        if self._window_start != start:
            self._window_start = start
            self._spent = {}

    # -- loading --------------------------------------------------------

    async def load(self) -> None:
        """Re-read limits and prices from the DB, then re-seed spend."""
        rows = await self._db.list_proxy_key_limits()
        limits: dict[str, dict[str, float]] = {}
        for row in rows:
            kind = str(row["kind"])
            if kind not in LIMIT_KINDS:
                continue
            amount = float(row["amount"] or 0.0)
            if amount <= 0:
                continue     # <= 0 means unlimited
            limits.setdefault(str(row["proxy_key"]), {})[kind] = amount
        self._limits = limits
        self._prices = build_price_lookup(await self._db.get_all_model_prices())
        await self.seed()
        logger.info(
            "Key limits loaded: %d key(s) limited, window starts %s",
            len(self._limits), self.window_start().isoformat(),
        )

    async def seed(self, now: datetime | None = None) -> None:
        """Recompute per-key spend for the current window from hourly buckets."""
        current = now or self._now()
        start = self.window_start(current).strftime(HOUR_FMT)
        end = self.window_end(current).strftime(HOUR_FMT)
        rows = await self._db.query_usage_key_hourly(start, end)
        spent: dict[str, float] = {}
        for row in rows:
            key = str(row["proxy_key"])
            spent[key] = spent.get(key, 0.0) + self._row_cost(row)
        self._spent = spent
        self._window_start = self.window_start(current)

    def _row_cost(self, row: dict) -> float:
        return calculate_cost(
            model=str(row["model"]),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            cache_read_tokens=int(row["cache_read_tokens"] or 0),
            cache_creation_tokens=int(row["cache_creation_tokens"] or 0),
            cache_creation_5m_tokens=int(row["cache_creation_5m_tokens"] or 0),
            cache_creation_1h_tokens=int(row["cache_creation_1h_tokens"] or 0),
            web_search_requests=int(row["web_search_requests"] or 0),
            prices=self._prices,
        ).total_cost or 0.0

    # -- request path ---------------------------------------------------

    def check(self, proxy_key: str, now: datetime | None = None) -> LimitBlock | None:
        """Return the blocking limit, or None. Dict lookups only — no I/O."""
        current = now or self._now()
        self._roll_if_needed(current)
        limits = self._limits.get(proxy_key)
        if not limits:
            return None
        spent = self._spent.get(proxy_key, 0.0)
        for kind, amount in limits.items():
            if spent >= amount:
                return LimitBlock(
                    kind=kind,
                    label=LIMIT_KINDS[kind].label,
                    retry_after=self.retry_after(current),
                    limit_usd=amount,
                    spent_usd=spent,
                )
        return None

    def add(
        self,
        proxy_key: str,
        model: str,
        usage: tuple[int, ...],
        now: datetime | None = None,
    ) -> None:
        """Add one request's cost. ``usage`` is the 7-tuple from
        ``extract_usage`` / ``extract_usage_from_sse``."""
        self._roll_if_needed(now or self._now())
        padded = tuple(usage) + (0,) * (7 - len(usage))
        cost = calculate_cost(
            model=model,
            input_tokens=int(padded[0] or 0),
            output_tokens=int(padded[1] or 0),
            cache_read_tokens=int(padded[2] or 0),
            cache_creation_tokens=int(padded[3] or 0),
            cache_creation_5m_tokens=int(padded[4] or 0),
            cache_creation_1h_tokens=int(padded[5] or 0),
            web_search_requests=int(padded[6] or 0),
            prices=self._prices,
        ).total_cost
        if cost:
            self._spent[proxy_key] = self._spent.get(proxy_key, 0.0) + cost

    # -- configuration --------------------------------------------------

    async def set_limit(
        self, proxy_key: str, kind: str, amount: float | None
    ) -> None:
        """Persist a limit and apply it in-process, so the very next request
        sees it. ``None`` or ``<= 0`` removes the limit (unlimited)."""
        if kind not in LIMIT_KINDS:
            raise ValueError(f"unknown limit kind: {kind}")
        if amount is None or float(amount) <= 0:
            await self._db.delete_proxy_key_limit(proxy_key, kind)
            per_key = self._limits.get(proxy_key)
            if per_key is not None:
                per_key.pop(kind, None)
                if not per_key:
                    self._limits.pop(proxy_key, None)
            return
        await self._db.set_proxy_key_limit(proxy_key, kind, float(amount))
        self._limits.setdefault(proxy_key, {})[kind] = float(amount)

    def limits_for(self, proxy_key: str) -> dict[str, float]:
        return dict(self._limits.get(proxy_key) or {})

    def snapshot(self, proxy_key: str, now: datetime | None = None) -> dict[str, dict]:
        """Per-kind status for the dashboard and /_oauth_usage."""
        current = now or self._now()
        self._roll_if_needed(current)
        spent = round(self._spent.get(proxy_key, 0.0), 6)
        resets_at = self.window_end(current).astimezone(self._tz).isoformat()
        out: dict[str, dict] = {}
        for kind in LIMIT_KINDS:
            amount = (self._limits.get(proxy_key) or {}).get(kind)
            out[kind] = {
                "limit_usd": amount,
                "spent_usd": spent,
                "remaining_usd": None if amount is None else round(max(0.0, amount - spent), 6),
                "percent": None if not amount else round(spent / amount * 100, 1),
                "resets_at": resets_at,
                "exceeded": bool(amount is not None and spent >= amount),
            }
        return out
