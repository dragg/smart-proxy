"""Token usage tracking and cost estimation.

Extracts token counts from OpenAI / Gemini API responses (both streaming
and non-streaming), accumulates them in memory, and periodically flushes
aggregated daily stats to SQLite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_ANTHROPIC_WEB_SEARCH_USD_PER_1K = 10.0

# $ per 1 million tokens: (input, output)
# Seed defaults for model_prices DB table.
_DEFAULT_MODEL_PRICES: dict[str, tuple[float, float]] = {
    # OpenAI — GPT-5 family
    "gpt-5.4-mini":   (0.25, 2.00),
    "gpt-5.4-nano":   (0.05, 0.40),
    "gpt-5.4":        (2.50, 15.00),
    "gpt-5.3-codex":  (2.50, 10.00),
    "gpt-5-mini":     (0.25, 2.00),
    "gpt-5-nano":     (0.05, 0.40),
    # OpenAI — GPT-4 family
    "gpt-4.1-mini":   (0.40, 1.60),
    "gpt-4.1-nano":   (0.10, 0.40),
    "gpt-4.1":        (2.00, 8.00),
    "gpt-4o-mini":    (0.15, 0.60),
    "gpt-4o":         (2.50, 10.00),
    # OpenAI — reasoning
    "o3-mini":        (1.10, 4.40),
    "o4-mini":        (1.10, 4.40),
    "o3":             (2.00, 8.00),
    # OpenAI — other
    "gpt-image-1":    (5.00, 40.00),
    "gpt-image-1.5":  (5.00, 10.00),
    "gpt-image-2":    (5.00, 30.00),
    "codex-mini":     (1.50, 6.00),
    # Gemini
    "gemini-2.5-pro":   (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-3.1-pro":   (1.25, 10.00),
    "gemini-3-pro":     (1.25, 10.00),
    # Anthropic
    "claude-fable-5":         (10.00, 50.00),
    "claude-mythos-5":        (10.00, 50.00),
    "claude-opus-4-8":        (5.00, 25.00),
    "claude-opus-4-7":        (5.00, 25.00),
    "claude-opus-4-6":        (5.00, 25.00),
    "claude-opus-4-5":        (5.00, 25.00),
    "claude-opus-4.6":        (5.00, 25.00),
    "claude-opus-4.5":        (5.00, 25.00),
    "claude-opus-4.1":        (15.00, 75.00),
    "claude-opus-4":          (15.00, 75.00),
    "claude-sonnet-5":        (3.00, 15.00),
    "claude-sonnet-4-6":      (3.00, 15.00),
    "claude-sonnet-4-5":      (3.00, 15.00),
    "claude-sonnet-4.6":      (3.00, 15.00),
    "claude-sonnet-4.5":      (3.00, 15.00),
    "claude-sonnet-4":        (3.00, 15.00),
    "claude-sonnet-3.7":      (3.00, 15.00),
    "claude-3.5-sonnet":      (3.00, 15.00),
    "claude-3-5-sonnet":      (3.00, 15.00),
    "claude-haiku-4-5":       (1.00, 5.00),
    "claude-haiku-4.5":       (1.00, 5.00),
    "claude-haiku-4":         (1.00, 5.00),
    "claude-3.5-haiku":       (0.80, 4.00),
    "claude-3-5-haiku":       (0.80, 4.00),
    "claude-haiku-3":         (0.25, 1.25),
}

# Anthropic prompt caching prices:
# (input, output, cache_read, cache_write_5m, cache_write_1h)
_DEFAULT_ANTHROPIC_CACHE_PRICES: dict[str, tuple[float, float, float, float, float]] = {
    "claude-fable-5":    (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-mythos-5":   (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-opus-4-8":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-7":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-6":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-5":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4.6":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4.5":   (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4.1":   (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-opus-4":     (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-sonnet-5":   (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4.6": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4.5": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4":   (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-3.7": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-3.5-sonnet": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-3-5-sonnet": (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-haiku-4-5":  (1.00, 5.00, 0.10, 1.25, 2.00),
    "claude-haiku-4.5":  (1.00, 5.00, 0.10, 1.25, 2.00),
    "claude-haiku-4":    (1.00, 5.00, 0.10, 1.25, 2.00),
    "claude-3.5-haiku":  (0.80, 4.00, 0.08, 1.00, 1.60),
    "claude-3-5-haiku":  (0.80, 4.00, 0.08, 1.00, 1.60),
    "claude-haiku-3":    (0.25, 1.25, 0.03, 0.30, 0.50),
}

# Backward-compatible aliases for callers/tests that import these names.
MODEL_PRICES = _DEFAULT_MODEL_PRICES
ANTHROPIC_CACHE_PRICES = _DEFAULT_ANTHROPIC_CACHE_PRICES


@dataclass(frozen=True)
class CostBreakdown:
    model: str
    matched_prefix: str | None
    provider: str | None
    input_cost: float
    output_cost: float
    cache_read_cost: float
    cache_write_5m_cost: float
    cache_write_1h_cost: float
    web_search_cost: float
    total_cost: float | None
    partial: bool
    unknown_pricing: bool


def _default_price_lookup() -> dict:
    return {
        "base_prices": dict(_DEFAULT_MODEL_PRICES),
        "cache_prices": dict(_DEFAULT_ANTHROPIC_CACHE_PRICES),
        "base_prefixes": sorted(_DEFAULT_MODEL_PRICES.keys(), key=len, reverse=True),
        "cache_prefixes": sorted(_DEFAULT_ANTHROPIC_CACHE_PRICES.keys(), key=len, reverse=True),
        "provider_by_prefix": {
            prefix: "anthropic" if prefix in _DEFAULT_ANTHROPIC_CACHE_PRICES else "generic"
            for prefix in _DEFAULT_MODEL_PRICES
        },
    }


def build_default_price_rows() -> list[tuple]:
    """Return default rows for db.seed_model_prices()."""
    rows: list[tuple] = []
    for prefix, (inp_price, out_price) in _DEFAULT_MODEL_PRICES.items():
        provider = "openai"
        if prefix.startswith("gemini-"):
            provider = "gemini"
        elif prefix.startswith("claude-"):
            provider = "anthropic"
        cache = _DEFAULT_ANTHROPIC_CACHE_PRICES.get(prefix)
        if cache:
            _, _, cache_read, cache_w5, cache_w1 = cache
            rows.append((prefix, provider, inp_price, out_price, cache_read, cache_w5, cache_w1))
        else:
            rows.append((prefix, provider, inp_price, out_price, None, None, None))
    return rows


def build_price_lookup(db_rows: list[dict]) -> dict:
    """Build prefix-sorted price lookup from model_prices rows."""
    base_prices: dict[str, tuple[float, float]] = {}
    cache_prices: dict[str, tuple[float, float, float, float, float]] = {}
    provider_by_prefix: dict[str, str] = {}

    for row in db_rows:
        prefix = row["model_prefix"]
        inp = float(row["input_price"])
        out = float(row["output_price"])
        base_prices[prefix] = (inp, out)
        provider_by_prefix[prefix] = row.get("provider") or "generic"

        cache_read = row.get("cache_read_price")
        cache_w5 = row.get("cache_write_5m_price")
        cache_w1 = row.get("cache_write_1h_price")
        if cache_read is not None and cache_w5 is not None and cache_w1 is not None:
            cache_prices[prefix] = (inp, out, float(cache_read), float(cache_w5), float(cache_w1))

    return {
        "base_prices": base_prices,
        "cache_prices": cache_prices,
        "base_prefixes": sorted(base_prices.keys(), key=len, reverse=True),
        "cache_prefixes": sorted(cache_prices.keys(), key=len, reverse=True),
        "provider_by_prefix": provider_by_prefix,
    }


def _resolve_price_lookup(prices: dict | None) -> dict:
    return prices if prices is not None else _default_price_lookup()


# A bare/legacy prefix (e.g. "claude-opus-4") is only allowed to match a model
# string that continues with a dated snapshot suffix ("-20250514[...]") or a
# non-numeric continuation ("-latest"). Anything else left over after the
# prefix — most importantly a short numeric continuation like "-7" or "-8" —
# means the model is actually an unlisted, more specific version, and must
# not be silently priced as the old generic entry.
_DATED_SNAPSHOT_SUFFIX_RE = re.compile(r"^-\d{8}(-.*)?$")

# Parses Claude model prefixes of the form "claude-<tier>[-.]<version digits>"
# into (family, version) so unrecognized versions can fall back to the latest
# *known* entry in the same family instead of an unrelated generic prefix.
_CLAUDE_FAMILY_RE = re.compile(r"^(claude-(?:opus|sonnet|haiku|fable|mythos))(?:[.\-](.*))?$")


def _is_confident_suffix(suffix: str) -> bool:
    if suffix == "":
        return True
    if _DATED_SNAPSHOT_SUFFIX_RE.match(suffix):
        return True
    return suffix[0] == "-" and not suffix[1:2].isdigit()


def _claude_family_version(prefix: str) -> tuple[str, tuple[int, ...]] | None:
    match = _CLAUDE_FAMILY_RE.match(prefix)
    if not match:
        return None
    family, rest = match.group(1), match.group(2) or ""
    version = tuple(int(n) for n in re.findall(r"\d+", rest))
    return family, version


def _resolve_matched_prefix(model: str, prefixes: list[str]) -> str | None:
    """Pick the price-table prefix that best describes `model`.

    `prefixes` must be sorted longest-first. Three passes, in order:

    1. Exact match.
    2. `model.startswith(prefix)` where the remainder confidently continues
       the same version (empty, a dated snapshot, or non-numeric) rather than
       an unseen version number ("claude-opus-4" must not swallow
       "claude-opus-4-8").
    3. Family fallback: for an unrecognized Claude model, use the newest
       known price entry in the same tier (e.g. "claude-opus-4-8" prices like
       the latest known Opus entry) rather than falling through to an
       unrelated generic/legacy prefix.
    """
    if model in prefixes:
        return model

    for prefix in prefixes:
        if model.startswith(prefix) and _is_confident_suffix(model[len(prefix):]):
            return prefix

    target = _claude_family_version(model)
    if target is None:
        return None
    target_family = target[0]

    best_prefix: str | None = None
    best_version: tuple[int, ...] | None = None
    for prefix in prefixes:
        candidate = _claude_family_version(prefix)
        if candidate is None or candidate[0] != target_family:
            continue
        if best_version is None or candidate[1] > best_version:
            best_version = candidate[1]
            best_prefix = prefix
    return best_prefix


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    web_search_requests: int = 0,
    prices: dict | None = None,
) -> CostBreakdown:
    """Return structured cost breakdown for a model usage tuple."""
    lookup = _resolve_price_lookup(prices)
    cache_prices: dict[str, tuple[float, float, float, float, float]] = lookup["cache_prices"]
    base_prices: dict[str, tuple[float, float]] = lookup["base_prices"]
    provider_by_prefix: dict[str, str] = lookup["provider_by_prefix"]

    cache_prefix = _resolve_matched_prefix(model, lookup["cache_prefixes"])
    if cache_prefix is not None:
        inp_price, out_price, read_price, write_5m_price, write_1h_price = cache_prices[cache_prefix]
        input_cost = (input_tokens * inp_price) / 1_000_000
        output_cost = (output_tokens * out_price) / 1_000_000
        cache_read_cost = (cache_read_tokens * read_price) / 1_000_000
        cache_write_5m_cost = 0.0
        cache_write_1h_cost = 0.0
        web_search_cost = 0.0
        partial = False

        split_total = cache_creation_5m_tokens + cache_creation_1h_tokens
        if split_total:
            cache_write_5m_cost += (cache_creation_5m_tokens * write_5m_price) / 1_000_000
            cache_write_1h_cost += (cache_creation_1h_tokens * write_1h_price) / 1_000_000
            if cache_creation_tokens > split_total:
                partial = True
                cache_write_5m_cost += ((cache_creation_tokens - split_total) * write_5m_price) / 1_000_000
        elif cache_creation_tokens:
            partial = True
            cache_write_5m_cost += (cache_creation_tokens * write_5m_price) / 1_000_000

        if provider_by_prefix.get(cache_prefix) == "anthropic" and web_search_requests > 0:
            web_search_cost = (web_search_requests * _ANTHROPIC_WEB_SEARCH_USD_PER_1K) / 1000

        total = (
            input_cost
            + output_cost
            + cache_read_cost
            + cache_write_5m_cost
            + cache_write_1h_cost
            + web_search_cost
        )
        return CostBreakdown(
            model=model,
            matched_prefix=cache_prefix,
            provider=provider_by_prefix.get(cache_prefix),
            input_cost=input_cost,
            output_cost=output_cost,
            cache_read_cost=cache_read_cost,
            cache_write_5m_cost=cache_write_5m_cost,
            cache_write_1h_cost=cache_write_1h_cost,
            web_search_cost=web_search_cost,
            total_cost=total,
            partial=partial,
            unknown_pricing=False,
        )

    base_prefix = _resolve_matched_prefix(model, lookup["base_prefixes"])
    if base_prefix is not None:
        inp_price, out_price = base_prices[base_prefix]
        input_cost = (input_tokens * inp_price) / 1_000_000
        output_cost = (output_tokens * out_price) / 1_000_000
        total = input_cost + output_cost
        return CostBreakdown(
            model=model,
            matched_prefix=base_prefix,
            provider=provider_by_prefix.get(base_prefix),
            input_cost=input_cost,
            output_cost=output_cost,
            cache_read_cost=0.0,
            cache_write_5m_cost=0.0,
            cache_write_1h_cost=0.0,
            web_search_cost=0.0,
            total_cost=total,
            partial=False,
            unknown_pricing=False,
        )

    return CostBreakdown(
        model=model,
        matched_prefix=None,
        provider=None,
        input_cost=0.0,
        output_cost=0.0,
        cache_read_cost=0.0,
        cache_write_5m_cost=0.0,
        cache_write_1h_cost=0.0,
        web_search_cost=0.0,
        total_cost=None,
        partial=False,
        unknown_pricing=True,
    )


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: dict | None = None,
) -> float | None:
    """Return estimated cost in USD, or None if model is unknown."""
    result = calculate_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        prices=prices,
    )
    return result.total_cost


def estimate_cost_with_cache(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
    web_search_requests: int = 0,
    prices: dict | None = None,
) -> tuple[float | None, bool]:
    """Return estimated cost and partial flag.

    For Anthropic models, includes cache read/write pricing.
    If only aggregated cache_creation_tokens is available (legacy rows),
    the write part is estimated using 5m write pricing and ``partial=True``.
    """
    result = calculate_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_creation_5m_tokens=cache_creation_5m_tokens,
        cache_creation_1h_tokens=cache_creation_1h_tokens,
        web_search_requests=web_search_requests,
        prices=prices,
    )
    return result.total_cost, result.partial


# ---------------------------------------------------------------------------
# Usage extraction — non-streaming JSON response
# ---------------------------------------------------------------------------

def _extract_anthropic_usage_fields(usage: dict) -> tuple[int, int, int, int, int, int, int]:
    """Extract Anthropic usage fields including cache split by TTL."""
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    cache_creation = usage.get("cache_creation") or {}
    cache_create_5m = cache_creation.get("ephemeral_5m_input_tokens", 0) or 0
    cache_create_1h = cache_creation.get("ephemeral_1h_input_tokens", 0) or 0
    server_tool_use = usage.get("server_tool_use") or {}
    web_search_requests = server_tool_use.get("web_search_requests", 0) or 0
    return (
        inp,
        out,
        cache_read,
        cache_create,
        cache_create_5m,
        cache_create_1h,
        web_search_requests,
    )


def extract_usage(provider: str, body: bytes) -> UsageResult | None:
    """Parse a full JSON response body.

    Returns None if the response doesn't contain usage data.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None

    if provider == "gemini":
        meta = data.get("usageMetadata")
        if not meta:
            return None
        return (
            meta.get("promptTokenCount", 0),
            meta.get("candidatesTokenCount", 0),
            0,
            0,
            0,
            0,
            0,
        )

    if provider == "anthropic":
        usage = data.get("usage")
        if not usage:
            return None
        return _extract_anthropic_usage_fields(usage)

    usage = data.get("usage")
    if not usage:
        return None
    inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    return (inp, out, 0, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Usage extraction — SSE stream tail
# ---------------------------------------------------------------------------

_DATA_RE = re.compile(rb"^data: ({.+})$", re.MULTILINE)


UsageResult = tuple[int, int, int, int, int, int, int]  # (input, output, cache_read, cache_creation, cache_create_5m, cache_create_1h, web_search_requests)


def extract_usage_from_sse(provider: str, buf: bytes) -> UsageResult | None:
    """Scan an SSE buffer for usage events.

    Returns (input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
    cache_creation_5m_tokens, cache_creation_1h_tokens).
    cache_read/cache_creation/web_search are Anthropic-only (0 for other providers).
    """
    last_inp = 0
    last_out = 0
    last_cache_read = 0
    last_cache_create = 0
    last_cache_create_5m = 0
    last_cache_create_1h = 0
    last_web_search = 0
    found = False

    for m in _DATA_RE.finditer(buf):
        try:
            obj = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue

        if provider == "gemini":
            meta = obj.get("usageMetadata")
            if meta:
                last_inp = meta.get("promptTokenCount", 0)
                last_out = meta.get("candidatesTokenCount", 0)
                found = True
        elif provider == "anthropic":
            msg_usage = obj.get("message", {}).get("usage")
            if msg_usage:
                (
                    last_inp,
                    _msg_out,
                    last_cache_read,
                    last_cache_create,
                    last_cache_create_5m,
                    last_cache_create_1h,
                    last_web_search,
                ) = _extract_anthropic_usage_fields(msg_usage)
                found = True
            usage = obj.get("usage")
            if usage:
                (
                    inp,
                    out,
                    cache_read,
                    cache_create,
                    cache_create_5m,
                    cache_create_1h,
                    web_search_requests,
                ) = _extract_anthropic_usage_fields(usage)
                if out:
                    last_out = out
                    found = True
                if inp and not last_inp:
                    last_inp = inp
                    found = True
                if cache_read:
                    last_cache_read = cache_read
                    found = True
                if cache_create:
                    last_cache_create = cache_create
                    found = True
                if cache_create_5m:
                    last_cache_create_5m = cache_create_5m
                    found = True
                if cache_create_1h:
                    last_cache_create_1h = cache_create_1h
                    found = True
                if web_search_requests:
                    last_web_search = web_search_requests
                    found = True
        else:
            usage = obj.get("usage")
            if usage:
                inp = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
                if inp or out:
                    last_inp, last_out = inp, out
                    found = True

    return (
        last_inp,
        last_out,
        last_cache_read,
        last_cache_create,
        last_cache_create_5m,
        last_cache_create_1h,
        last_web_search,
    ) if found else None


# ---------------------------------------------------------------------------
# In-memory accumulator
# ---------------------------------------------------------------------------

# Buffer key: (date, proxy_key, group_name, credential_id, provider, model, via_openai_compat)
_BufKey = tuple[str, str, str, str, str, str, int]
# Bucket key: the daily key with the hour label in place of the date, plus
# request_kind -- so usage_daily and usage_kind_daily are both projections.
_BucketKey = tuple[str, str, str, str, str, str, int, str]

_TAIL_BUF_MAX = 8192


class UsageFlushError(Exception):
    """One or more usage tables failed to flush; every table was still attempted.

    ``failures`` maps table name -> exception. ``rows`` holds the usage_daily
    rows that DID land (empty when the usage_daily+usage_bucket pair failed),
    so the caller can still attribute committed usage to OAuth windows.
    """

    def __init__(self, failures: dict[str, Exception], rows: list[tuple]) -> None:
        self.failures = failures
        self.rows = rows
        detail = "; ".join(f"{name}: {exc!r}" for name, exc in failures.items())
        super().__init__(f"usage flush failed for {', '.join(failures)} -- {detail}")


class UsageTracker:
    """Accumulates per-request token usage in memory and flushes to DB."""

    def __init__(self) -> None:
        # values: [input, output, cache_read, cache_creation, cache_creation_5m, cache_creation_1h, web_search_requests, requests]
        self._buf: dict[_BufKey, list[int]] = {}
        # kind key: (date, proxy_key, request_kind, provider, model) -> 8 counters
        self._kind_buf: dict[tuple, list[int]] = {}
        # session key: (session_id, proxy_key, request_kind, provider, model)
        #   -> [first_date, last_date, project, title, 8 counters]
        self._session_buf: dict[tuple, list] = {}
        # hour key: (hour_utc, proxy_key, model) -> 8 counters
        self._hour_buf: dict[tuple, list[int]] = {}
        # bucket key: (hour_utc, proxy_key, group, credential_id, provider,
        #   model, via_openai_compat, request_kind) -> 8 counters
        self._bucket_buf: dict[_BucketKey, list[int]] = {}
        self._lock = asyncio.Lock()

    def _accumulate(self, buf: dict, key: tuple, counters: tuple) -> None:
        """Accumulate counter values into a buffer by key."""
        acc = buf.get(key)
        if acc is None:
            acc = [0] * 8
            buf[key] = acc
        for i, c in enumerate(counters):
            acc[i] += c

    def record(
        self,
        proxy_key: str,
        credential_id: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_creation_5m_tokens: int = 0,
        cache_creation_1h_tokens: int = 0,
        web_search_requests: int = 0,
        group_name: str | None = None,
        via_openai_compat: bool = False,
        request_kind: str = "unknown",
        session_id: str = "",
        project: str = "",
        title: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        date = now.strftime("%Y-%m-%d")
        hour = now.strftime("%Y-%m-%dT%H")
        group = (group_name or "").strip()
        key = (date, proxy_key, group, credential_id, provider, model, int(via_openai_compat))
        acc = self._buf.get(key)
        if acc is None:
            acc = [0, 0, 0, 0, 0, 0, 0, 0]
            self._buf[key] = acc
        acc[0] += input_tokens
        acc[1] += output_tokens
        acc[2] += cache_read_tokens
        acc[3] += cache_creation_tokens
        acc[4] += cache_creation_5m_tokens
        acc[5] += cache_creation_1h_tokens
        acc[6] += web_search_requests
        acc[7] += 1

        counters = (input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                    cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, 1)
        kkey = (date, proxy_key, request_kind or "unknown", provider, model)
        self._accumulate(self._kind_buf, kkey, counters)
        hkey = (hour, proxy_key, model)
        self._accumulate(self._hour_buf, hkey, counters)
        # `hour` and `date` come from the one `now` above, so hour[:10] == date
        # holds for every record -- that is what makes usage_daily a projection
        # of usage_bucket rather than a second, drifting count.
        bkey = (hour, proxy_key, group, credential_id, provider, model,
                int(via_openai_compat), request_kind or "unknown")
        self._accumulate(self._bucket_buf, bkey, counters)
        if session_id:
            skey = (session_id, proxy_key, request_kind or "unknown", provider, model)
            sacc = self._session_buf.get(skey)
            if sacc is None:
                sacc = [date, date, "", "", 0, 0, 0, 0, 0, 0, 0, 0]
                self._session_buf[skey] = sacc
            sacc[0] = min(sacc[0], date)
            sacc[1] = max(sacc[1], date)
            if project:
                sacc[2] = project
            if title:
                sacc[3] = title
            for i, c in enumerate(counters):
                sacc[4 + i] += c

    async def flush(self, db: object) -> list[tuple]:
        """Move buffered data to the database.

        ``usage_daily`` and ``usage_bucket`` are written in ONE transaction --
        the former is a projection of the latter, so they must never disagree.
        ``usage_key_hourly``, ``usage_kind_daily`` and ``usage_session`` are
        each written independently, so one table's failure no longer discards
        the others' already-swapped-out buffers. That was the 2026-08-21
        failure: a single broken write starved the spend limiter's re-seed
        source for hours while serving looked healthy.

        A failed table's buffer is still lost. Re-merging it was considered and
        rejected on 2026-08-21 (degraded-mode spec, section 3): during a long
        outage the buffer grows without bound.

        Raises :class:`UsageFlushError` *after* attempting every write if any
        of them failed. Returns the flushed ``usage_daily`` row tuples:
        (date, proxy_key, group_name, credential_id, provider, model,
        via_openai_compat, input, output, cache_read, cache_creation,
        cache_creation_5m, cache_creation_1h, web_search_requests, requests).
        """
        from smart_proxy.db import Database
        assert isinstance(db, Database)

        async with self._lock:
            if not self._buf:
                return []
            snapshot, self._buf = self._buf, {}
            kind_snapshot, self._kind_buf = self._kind_buf, {}
            session_snapshot, self._session_buf = self._session_buf, {}
            hour_snapshot, self._hour_buf = self._hour_buf, {}
            bucket_snapshot, self._bucket_buf = self._bucket_buf, {}

        rows = [
            (
                k[0], k[1], (k[2] or None), k[3], k[4], k[5], k[6],
                v[0], v[1], v[2], v[3], v[4], v[5], v[6], v[7],
            )
            for k, v in snapshot.items()
        ]
        bucket_rows = [
            (k[0], k[1], (k[2] or None), k[3], k[4], k[5], k[6], k[7], *v)
            for k, v in bucket_snapshot.items()
        ]
        kind_rows = [(k[0], k[1], k[2], k[3], k[4], *v) for k, v in kind_snapshot.items()]
        session_rows = [
            (k[0], k[1], k[2], k[3], k[4],   # session_id, proxy_key, request_kind, provider, model
             v[0], v[1], v[2], v[3],          # first_date, last_date, project, title
             *v[4:])                          # 8 counters
            for k, v in session_snapshot.items()
        ]
        hour_rows = [(k[0], k[1], k[2], *v) for k, v in hour_snapshot.items()]

        failures: dict[str, Exception] = {}
        landed: list[tuple] = []
        try:
            await db.upsert_usage_daily_and_bucket_batch(rows, bucket_rows)
            landed = rows
        except Exception as exc:
            failures["usage_daily+usage_bucket"] = exc

        # The spend limiter's re-seed source goes first among the independents.
        for name, write, batch in (
            ("usage_key_hourly", db.upsert_usage_hourly_batch, hour_rows),
            ("usage_kind_daily", db.upsert_usage_kind_batch, kind_rows),
            ("usage_session", db.upsert_usage_session_batch, session_rows),
        ):
            try:
                await write(batch)
            except Exception as exc:
                failures[name] = exc

        if failures:
            raise UsageFlushError(failures, landed)
        logger.debug("Flushed %d usage rows to DB", len(rows))
        return rows
