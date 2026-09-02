from __future__ import annotations

from contextlib import asynccontextmanager

import json
import logging
from datetime import datetime, timezone

import aiosqlite
try:
    import psycopg
except ImportError:  # pragma: no cover - optional until postgres backend is installed
    psycopg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Single-step utilization fall (percentage points) treated as an undeclared
# window reset rather than the gradual decline of a rolling window.
UTILIZATION_DROP_THRESHOLD_PP = 5.0

# Upstream jitters resets_at by minutes between polls of the same window;
# an observation within this distance of the latest known window is treated
# as that window, not a reset. Negligible against multi-hour/-day windows.
RESETS_AT_JITTER_TOLERANCE_MINUTES = 120.0

# ``seven_day`` (top-level) and ``limit:weekly_all`` are the same upstream
# counter reported twice; their drop histories are byte-identical in
# production. Collapse them so one wipe is one row, not two.
WINDOW_KIND_ALIASES: dict[str, str] = {"limit:weekly_all": "seven_day"}


def canonical_window_kind(kind: str) -> str:
    """Map a recorded window_kind onto its logical counter."""
    return WINDOW_KIND_ALIASES.get(kind, kind)


def is_weekly_window_kind(kind: str) -> bool:
    """True for the multi-day counters a wipe can apply to.

    ``five_hour``/``limit:session`` are excluded deliberately: those reach 0 at
    every ordinary rollover, which is not a wipe.
    """
    return kind == "seven_day" or kind.startswith("limit:weekly")
_FERNET_TOKEN_PREFIX = "gAAAA"
_INTEGRITY_ERRORS: tuple[type[Exception], ...] = (aiosqlite.IntegrityError,)
if psycopg is not None:
    _INTEGRITY_ERRORS = _INTEGRITY_ERRORS + (psycopg.IntegrityError,)


def _iso_minutes_between(a_iso: str, b_iso: str) -> float | None:
    """Signed distance from a to b in minutes; None if unparseable."""
    try:
        a_dt = datetime.fromisoformat(a_iso)
        b_dt = datetime.fromisoformat(b_iso)
        return (b_dt - a_dt).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def _parse_iso_utc(raw: str | None) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso_span_days(prev_iso: str, new_iso: str) -> float | None:
    """Distance between two ISO timestamps in days, 2 decimals; None if unparseable."""
    try:
        prev_dt = datetime.fromisoformat(prev_iso)
        new_dt = datetime.fromisoformat(new_iso)
        return round((new_dt - prev_dt).total_seconds() / 86400.0, 2)
    except (ValueError, TypeError):
        return None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS proxy_api_keys (
    key        TEXT PRIMARY KEY,
    name       TEXT    NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_daily (
    date                  TEXT    NOT NULL,
    proxy_key             TEXT    NOT NULL DEFAULT '',
    group_name            TEXT,
    credential_id         TEXT    NOT NULL,
    provider              TEXT    NOT NULL,
    model                 TEXT    NOT NULL,
    via_openai_compat     INTEGER NOT NULL DEFAULT 0,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests   INTEGER NOT NULL DEFAULT 0,
    requests              INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, proxy_key, credential_id, provider, model, via_openai_compat)
);
CREATE INDEX IF NOT EXISTS idx_usage_daily_date ON usage_daily(date);

CREATE TABLE IF NOT EXISTS usage_kind_daily (
    date         TEXT    NOT NULL,
    proxy_key    TEXT    NOT NULL DEFAULT '',
    request_kind TEXT    NOT NULL DEFAULT 'unknown',
    provider     TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, proxy_key, request_kind, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_kind_daily_date ON usage_kind_daily(date);
CREATE TABLE IF NOT EXISTS usage_session (
    session_id   TEXT    NOT NULL,
    proxy_key    TEXT    NOT NULL DEFAULT '',
    request_kind TEXT    NOT NULL DEFAULT 'unknown',
    provider     TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    first_date   TEXT    NOT NULL,
    last_date    TEXT    NOT NULL,
    project      TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, proxy_key, request_kind, provider, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_session_key ON usage_session(proxy_key);

CREATE TABLE IF NOT EXISTS model_prices (
    model_prefix         TEXT PRIMARY KEY,
    provider             TEXT NOT NULL,
    input_price          REAL NOT NULL,
    output_price         REAL NOT NULL,
    cache_read_price     REAL,
    cache_write_5m_price REAL,
    cache_write_1h_price REAL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS anthropic_keys (
    id                TEXT PRIMARY KEY,
    key_type          TEXT    NOT NULL,  -- 'oauth' or 'api_key'
    status            TEXT    NOT NULL DEFAULT 'active',
    api_key           TEXT,
    access_token      TEXT,
    refresh_token     TEXT,
    client_id         TEXT    DEFAULT '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
    expires_at        INTEGER,  -- epoch milliseconds
    scopes            TEXT    DEFAULT '[]',
    subscription_type TEXT    DEFAULT '',
    rate_limit_tier   TEXT    DEFAULT '',
    name              TEXT    NOT NULL DEFAULT '',
    role              TEXT    NOT NULL DEFAULT 'primary',
    -- JSON array of full proxy keys allowed to escalate onto this key. Only
    -- meaningful for role='fallback'; '[]' means nobody (fail-closed).
    allowed_proxy_keys TEXT   NOT NULL DEFAULT '[]',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS anthropic_key_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id             TEXT    NOT NULL,
    snapshot_kind      TEXT    NOT NULL,
    trigger_event_type TEXT    NOT NULL DEFAULT '',
    status             TEXT    NOT NULL DEFAULT '',
    key_type           TEXT    NOT NULL DEFAULT '',
    name               TEXT    NOT NULL DEFAULT '',
    client_id          TEXT    NOT NULL DEFAULT '',
    expires_at         INTEGER,
    access_token       TEXT,
    refresh_token      TEXT,
    scopes             TEXT    DEFAULT '[]',
    subscription_type  TEXT    DEFAULT '',
    rate_limit_tier    TEXT    DEFAULT '',
    row_json           TEXT    NOT NULL DEFAULT '{}',
    created_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aks_key_created ON anthropic_key_snapshots(key_id, created_at);

CREATE TABLE IF NOT EXISTS anthropic_key_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id         TEXT    NOT NULL DEFAULT '',
    key_id        TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT '',
    event_type    TEXT    NOT NULL,
    decision      TEXT    NOT NULL DEFAULT '',
    path          TEXT    NOT NULL DEFAULT '',
    model         TEXT,
    http_status   INTEGER,
    request_id    TEXT    NOT NULL DEFAULT '',
    error_type    TEXT    NOT NULL DEFAULT '',
    error_message TEXT    NOT NULL DEFAULT '',
    retry_after   INTEGER,
    snapshot_id   INTEGER,
    context_json  TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ake_key_created ON anthropic_key_events(key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ake_op_id ON anthropic_key_events(op_id);

CREATE TABLE IF NOT EXISTS rate_limit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT    NOT NULL,
    credential_id   TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,  -- ISO timestamp
    retry_after     INTEGER,           -- seconds from Anthropic
    reset_at        TEXT,              -- ISO timestamp when limit resets
    limit_type      TEXT    DEFAULT '',-- e.g. 'five_hour', 'seven_day'
    utilization_5h  REAL,              -- 5-hour window 0.0-1.0
    utilization_7d  REAL,              -- 7-day window 0.0-1.0
    requests_during INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_rll_provider_date ON rate_limit_log(provider, started_at);

CREATE TABLE IF NOT EXISTS oauth_window_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id             TEXT    NOT NULL,   -- anthropic_keys.id
    window_kind        TEXT    NOT NULL,   -- 'five_hour', 'seven_day', 'limit:weekly_scoped:Fable', ...
    resets_at          TEXT    NOT NULL,   -- minute-truncated ISO UTC; window identity
    resets_at_raw      TEXT    NOT NULL,   -- as last received from upstream
    first_seen_at      TEXT    NOT NULL,
    first_active_at    TEXT,               -- first observation with utilization > 0
    last_seen_at       TEXT    NOT NULL,
    observations       INTEGER NOT NULL DEFAULT 1,
    last_utilization   REAL,               -- percent 0-100
    max_utilization    REAL,
    max_utilization_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_owl_identity
    ON oauth_window_log(key_id, window_kind, resets_at);

CREATE TABLE IF NOT EXISTS oauth_window_drop_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id           TEXT NOT NULL,
    window_kind      TEXT NOT NULL,
    resets_at        TEXT NOT NULL,   -- what the API claimed at drop time
    dropped_at       TEXT NOT NULL,   -- observation that saw the low value
    prev_seen_at     TEXT NOT NULL,   -- when from_utilization was last seen
    from_utilization REAL NOT NULL,
    to_utilization   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_owdl_key
    ON oauth_window_drop_log(key_id, window_kind, dropped_at);

CREATE TABLE IF NOT EXISTS oauth_usage_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id        TEXT    NOT NULL,
    payload_hash  TEXT    NOT NULL,   -- sha256 of the canonical form
    payload_json  TEXT    NOT NULL,   -- raw upstream body, verbatim
    headers_json  TEXT    NOT NULL,   -- whitelisted upstream response headers
    first_seen_at TEXT    NOT NULL,
    last_seen_at  TEXT    NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ous_key_seen
    ON oauth_usage_snapshot(key_id, first_seen_at);

CREATE TABLE IF NOT EXISTS oauth_limit_wipe (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id                  TEXT    NOT NULL,
    window_kind             TEXT    NOT NULL,  -- logical kind, duplicates collapsed
    observed_at             TEXT    NOT NULL,
    prev_seen_at            TEXT    NOT NULL,
    from_utilization        REAL    NOT NULL,
    resets_at_claimed       TEXT    NOT NULL,
    hours_before_claimed    REAL,
    five_hour_rolled        INTEGER NOT NULL DEFAULT 0,
    five_hour_early_minutes REAL,
    source                  TEXT    NOT NULL DEFAULT 'poll',  -- 'poll' | 'headers'
    context_json            TEXT,
    snapshot_id             INTEGER,
    prev_snapshot_id        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_olw_key
    ON oauth_limit_wipe(key_id, observed_at);

CREATE TABLE IF NOT EXISTS oauth_window_usage (
    window_id                INTEGER NOT NULL,   -- oauth_window_log.id
    model                    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (window_id, model)
);

CREATE TABLE IF NOT EXISTS oauth_window_usage_pending (
    key_id                   TEXT    NOT NULL,
    window_kind              TEXT    NOT NULL,
    model                    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    updated_at               TEXT    NOT NULL,
    PRIMARY KEY (key_id, window_kind, model)
);

CREATE TABLE IF NOT EXISTS proxy_key_limits (
    proxy_key  TEXT NOT NULL,
    kind       TEXT NOT NULL,
    amount     REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (proxy_key, kind)
);

CREATE TABLE IF NOT EXISTS usage_key_hourly (
    hour_utc                 TEXT    NOT NULL,
    proxy_key                TEXT    NOT NULL,
    model                    TEXT    NOT NULL,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    requests                 INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_utc, proxy_key, model)
);
CREATE INDEX IF NOT EXISTS idx_usage_key_hourly_hour ON usage_key_hourly(hour_utc);
"""

MIGRATIONS = [
    # Decommissioned OpenAI/Gemini key subsystem: drop its tables if present.
    "DROP TABLE IF EXISTS key_model_status",
    "DROP TABLE IF EXISTS credentials",
    # usage_daily — for existing DBs that were created before this table existed
    """CREATE TABLE IF NOT EXISTS usage_daily (
        date          TEXT    NOT NULL,
        proxy_key     TEXT    NOT NULL DEFAULT '',
        credential_id TEXT    NOT NULL,
        provider      TEXT    NOT NULL,
        model         TEXT    NOT NULL,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        requests      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (date, proxy_key, credential_id, provider, model)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_usage_daily_date ON usage_daily(date)",
    # anthropic_keys — for existing DBs created before this table
    """CREATE TABLE IF NOT EXISTS anthropic_keys (
        id                TEXT PRIMARY KEY,
        key_type          TEXT    NOT NULL,
        status            TEXT    NOT NULL DEFAULT 'active',
        api_key           TEXT,
        access_token      TEXT,
        refresh_token     TEXT,
        client_id         TEXT    DEFAULT '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
        expires_at        INTEGER,
        scopes            TEXT    DEFAULT '[]',
        subscription_type TEXT    DEFAULT '',
        rate_limit_tier   TEXT    DEFAULT '',
        name              TEXT    NOT NULL DEFAULT '',
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS anthropic_key_snapshots (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id             TEXT    NOT NULL,
        snapshot_kind      TEXT    NOT NULL,
        trigger_event_type TEXT    NOT NULL DEFAULT '',
        status             TEXT    NOT NULL DEFAULT '',
        key_type           TEXT    NOT NULL DEFAULT '',
        name               TEXT    NOT NULL DEFAULT '',
        client_id          TEXT    NOT NULL DEFAULT '',
        expires_at         INTEGER,
        access_token       TEXT,
        refresh_token      TEXT,
        scopes             TEXT    DEFAULT '[]',
        subscription_type  TEXT    DEFAULT '',
        rate_limit_tier    TEXT    DEFAULT '',
        row_json           TEXT    NOT NULL DEFAULT '{}',
        created_at         TEXT    NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_aks_key_created ON anthropic_key_snapshots(key_id, created_at)",
    """CREATE TABLE IF NOT EXISTS anthropic_key_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        op_id         TEXT    NOT NULL DEFAULT '',
        key_id        TEXT    NOT NULL,
        source        TEXT    NOT NULL DEFAULT '',
        event_type    TEXT    NOT NULL,
        decision      TEXT    NOT NULL DEFAULT '',
        path          TEXT    NOT NULL DEFAULT '',
        model         TEXT,
        http_status   INTEGER,
        request_id    TEXT    NOT NULL DEFAULT '',
        error_type    TEXT    NOT NULL DEFAULT '',
        error_message TEXT    NOT NULL DEFAULT '',
        retry_after   INTEGER,
        snapshot_id   INTEGER,
        context_json  TEXT    NOT NULL DEFAULT '{}',
        created_at    TEXT    NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ake_key_created ON anthropic_key_events(key_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ake_op_id ON anthropic_key_events(op_id)",
    "ALTER TABLE usage_daily ADD COLUMN cache_read_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_daily ADD COLUMN cache_creation_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_daily ADD COLUMN cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_daily ADD COLUMN cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_daily ADD COLUMN web_search_requests INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE usage_daily ADD COLUMN group_name TEXT",
    """CREATE TABLE IF NOT EXISTS model_prices (
        model_prefix         TEXT PRIMARY KEY,
        provider             TEXT NOT NULL,
        input_price          REAL NOT NULL,
        output_price         REAL NOT NULL,
        cache_read_price     REAL,
        cache_write_5m_price REAL,
        cache_write_1h_price REAL,
        updated_at           TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS rate_limit_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        provider        TEXT    NOT NULL,
        credential_id   TEXT    NOT NULL,
        started_at      TEXT    NOT NULL,
        retry_after     INTEGER,
        reset_at        TEXT,
        limit_type      TEXT    DEFAULT '',
        utilization_5h  REAL,
        utilization_7d  REAL,
        requests_during INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rll_provider_date ON rate_limit_log(provider, started_at)",
    "ALTER TABLE rate_limit_log ADD COLUMN utilization_5h REAL",
    "ALTER TABLE rate_limit_log ADD COLUMN utilization_7d REAL",
    """CREATE TABLE IF NOT EXISTS oauth_window_log (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id             TEXT    NOT NULL,
        window_kind        TEXT    NOT NULL,
        resets_at          TEXT    NOT NULL,
        resets_at_raw      TEXT    NOT NULL,
        first_seen_at      TEXT    NOT NULL,
        first_active_at    TEXT,
        last_seen_at       TEXT    NOT NULL,
        observations       INTEGER NOT NULL DEFAULT 1,
        last_utilization   REAL,
        max_utilization    REAL,
        max_utilization_at TEXT
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_owl_identity
        ON oauth_window_log(key_id, window_kind, resets_at)""",
    """CREATE TABLE IF NOT EXISTS oauth_window_drop_log (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id           TEXT NOT NULL,
        window_kind      TEXT NOT NULL,
        resets_at        TEXT NOT NULL,
        dropped_at       TEXT NOT NULL,
        prev_seen_at     TEXT NOT NULL,
        from_utilization REAL NOT NULL,
        to_utilization   REAL NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_owdl_key
        ON oauth_window_drop_log(key_id, window_kind, dropped_at)""",
    """CREATE TABLE IF NOT EXISTS oauth_usage_snapshot (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id        TEXT    NOT NULL,
        payload_hash  TEXT    NOT NULL,
        payload_json  TEXT    NOT NULL,
        headers_json  TEXT    NOT NULL,
        first_seen_at TEXT    NOT NULL,
        last_seen_at  TEXT    NOT NULL,
        seen_count    INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE INDEX IF NOT EXISTS idx_ous_key_seen
        ON oauth_usage_snapshot(key_id, first_seen_at)""",
    """CREATE TABLE IF NOT EXISTS oauth_limit_wipe (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        key_id                  TEXT    NOT NULL,
        window_kind             TEXT    NOT NULL,
        observed_at             TEXT    NOT NULL,
        prev_seen_at            TEXT    NOT NULL,
        from_utilization        REAL    NOT NULL,
        resets_at_claimed       TEXT    NOT NULL,
        hours_before_claimed    REAL,
        five_hour_rolled        INTEGER NOT NULL DEFAULT 0,
        five_hour_early_minutes REAL,
        source                  TEXT    NOT NULL DEFAULT 'poll',
        context_json            TEXT,
        snapshot_id             INTEGER,
        prev_snapshot_id        INTEGER
    )""",
    """CREATE INDEX IF NOT EXISTS idx_olw_key
        ON oauth_limit_wipe(key_id, observed_at)""",
    """CREATE TABLE IF NOT EXISTS oauth_window_usage (
        window_id                INTEGER NOT NULL,
        model                    TEXT    NOT NULL,
        input_tokens             INTEGER NOT NULL DEFAULT 0,
        output_tokens            INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
        cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
        web_search_requests      INTEGER NOT NULL DEFAULT 0,
        requests                 INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (window_id, model)
    )""",
    """CREATE TABLE IF NOT EXISTS oauth_window_usage_pending (
        key_id                   TEXT    NOT NULL,
        window_kind              TEXT    NOT NULL,
        model                    TEXT    NOT NULL,
        input_tokens             INTEGER NOT NULL DEFAULT 0,
        output_tokens            INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
        cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
        web_search_requests      INTEGER NOT NULL DEFAULT 0,
        requests                 INTEGER NOT NULL DEFAULT 0,
        updated_at               TEXT    NOT NULL,
        PRIMARY KEY (key_id, window_kind, model)
    )""",
    """CREATE TABLE IF NOT EXISTS usage_kind_daily (
        date         TEXT    NOT NULL,
        proxy_key    TEXT    NOT NULL DEFAULT '',
        request_kind TEXT    NOT NULL DEFAULT 'unknown',
        provider     TEXT    NOT NULL,
        model        TEXT    NOT NULL,
        input_tokens             INTEGER NOT NULL DEFAULT 0,
        output_tokens            INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
        cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
        web_search_requests      INTEGER NOT NULL DEFAULT 0,
        requests                 INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (date, proxy_key, request_kind, provider, model)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_usage_kind_daily_date ON usage_kind_daily(date)",
    """CREATE TABLE IF NOT EXISTS usage_session (
        session_id   TEXT    NOT NULL,
        proxy_key    TEXT    NOT NULL DEFAULT '',
        request_kind TEXT    NOT NULL DEFAULT 'unknown',
        provider     TEXT    NOT NULL,
        model        TEXT    NOT NULL,
        first_date   TEXT    NOT NULL,
        last_date    TEXT    NOT NULL,
        project      TEXT    NOT NULL DEFAULT '',
        title        TEXT    NOT NULL DEFAULT '',
        input_tokens             INTEGER NOT NULL DEFAULT 0,
        output_tokens            INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
        cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
        cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
        web_search_requests      INTEGER NOT NULL DEFAULT 0,
        requests                 INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (session_id, proxy_key, request_kind, provider, model)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_usage_session_key ON usage_session(proxy_key)",
    "ALTER TABLE anthropic_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'primary'",
    "ALTER TABLE anthropic_keys ADD COLUMN allowed_proxy_keys TEXT NOT NULL DEFAULT '[]'",
]


def parse_allowed_proxy_keys(raw: object) -> frozenset[str]:
    """Parse ``anthropic_keys.allowed_proxy_keys`` into a set of full proxy keys.

    Fail-closed and never raises: NULL (code running before the migration),
    ``''``, malformed JSON and non-list payloads all yield the empty set, which
    means *nobody* may escalate onto the key.
    """
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(str(k).strip() for k in raw if str(k).strip())
    if not isinstance(raw, str) or not raw.strip():
        return frozenset()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return frozenset()
    if not isinstance(parsed, list):
        return frozenset()
    return frozenset(str(k).strip() for k in parsed if isinstance(k, str) and k.strip())

SNAPSHOT_TABLE_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "proxy_api_keys",
        ("key", "name", "active", "created_at"),
        "key",
    ),
    (
        "usage_daily",
        (
            "date",
            "proxy_key",
            "group_name",
            "credential_id",
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cache_creation_5m_tokens",
            "cache_creation_1h_tokens",
            "web_search_requests",
            "requests",
        ),
        "date, proxy_key, credential_id, provider, model",
    ),
    (
        "usage_kind_daily",
        (
            "date", "proxy_key", "request_kind", "provider", "model",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cache_creation_5m_tokens",
            "cache_creation_1h_tokens", "web_search_requests", "requests",
        ),
        "date, proxy_key, request_kind, provider, model",
    ),
    (
        "usage_session",
        (
            "session_id", "proxy_key", "request_kind", "provider", "model",
            "first_date", "last_date", "project", "title",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cache_creation_5m_tokens",
            "cache_creation_1h_tokens", "web_search_requests", "requests",
        ),
        "session_id, proxy_key, request_kind, provider, model",
    ),
    (
        "model_prices",
        (
            "model_prefix",
            "provider",
            "input_price",
            "output_price",
            "cache_read_price",
            "cache_write_5m_price",
            "cache_write_1h_price",
            "updated_at",
        ),
        "model_prefix",
    ),
    (
        "anthropic_keys",
        (
            "id",
            "key_type",
            "status",
            "api_key",
            "access_token",
            "refresh_token",
            "client_id",
            "expires_at",
            "scopes",
            "subscription_type",
            "rate_limit_tier",
            "name",
            # role/allowed_proxy_keys must round-trip: without them a restore
            # resurrects a scoped 'fallback' key as an unscoped 'primary' one,
            # i.e. a paid credential silently serving all traffic.
            "role",
            "allowed_proxy_keys",
            "created_at",
            "updated_at",
        ),
        "id",
    ),
    (
        "anthropic_key_snapshots",
        (
            "id",
            "key_id",
            "snapshot_kind",
            "trigger_event_type",
            "status",
            "key_type",
            "name",
            "client_id",
            "expires_at",
            "access_token",
            "refresh_token",
            "scopes",
            "subscription_type",
            "rate_limit_tier",
            "row_json",
            "created_at",
        ),
        "id",
    ),
    (
        "anthropic_key_events",
        (
            "id",
            "op_id",
            "key_id",
            "source",
            "event_type",
            "decision",
            "path",
            "model",
            "http_status",
            "request_id",
            "error_type",
            "error_message",
            "retry_after",
            "snapshot_id",
            "context_json",
            "created_at",
        ),
        "id",
    ),
    (
        "rate_limit_log",
        (
            "id",
            "provider",
            "credential_id",
            "started_at",
            "retry_after",
            "reset_at",
            "limit_type",
            "utilization_5h",
            "utilization_7d",
            "requests_during",
        ),
        "id",
    ),
    (
        "oauth_window_log",
        (
            "id",
            "key_id",
            "window_kind",
            "resets_at",
            "resets_at_raw",
            "first_seen_at",
            "first_active_at",
            "last_seen_at",
            "observations",
            "last_utilization",
            "max_utilization",
            "max_utilization_at",
        ),
        "id",
    ),
    (
        "oauth_window_drop_log",
        (
            "id",
            "key_id",
            "window_kind",
            "resets_at",
            "dropped_at",
            "prev_seen_at",
            "from_utilization",
            "to_utilization",
        ),
        "id",
    ),
    (
        "oauth_usage_snapshot",
        (
            "id",
            "key_id",
            "payload_hash",
            "payload_json",
            "headers_json",
            "first_seen_at",
            "last_seen_at",
            "seen_count",
        ),
        "id",
    ),
    (
        "oauth_limit_wipe",
        (
            "id",
            "key_id",
            "window_kind",
            "observed_at",
            "prev_seen_at",
            "from_utilization",
            "resets_at_claimed",
            "hours_before_claimed",
            "five_hour_rolled",
            "five_hour_early_minutes",
            "source",
            "context_json",
            "snapshot_id",
            "prev_snapshot_id",
        ),
        "id",
    ),
    (
        "oauth_window_usage",
        (
            "window_id", "model", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens",
            "cache_creation_5m_tokens", "cache_creation_1h_tokens",
            "web_search_requests", "requests",
        ),
        "window_id, model",
    ),
    (
        "oauth_window_usage_pending",
        (
            "key_id", "window_kind", "model", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens",
            "cache_creation_5m_tokens", "cache_creation_1h_tokens",
            "web_search_requests", "requests", "updated_at",
        ),
        "key_id, window_kind, model",
    ),
    (
        "proxy_key_limits",
        ("proxy_key", "kind", "amount", "updated_at"),
        "proxy_key, kind",
    ),
    (
        "usage_key_hourly",
        (
            "hour_utc", "proxy_key", "model",
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cache_creation_5m_tokens",
            "cache_creation_1h_tokens", "web_search_requests", "requests",
        ),
        "hour_utc, proxy_key, model",
    ),
)

# Import-time defaults for columns absent from older snapshots (their NOT NULL
# columns would otherwise get None → IntegrityError on restore).
_SNAPSHOT_COLUMN_DEFAULTS: dict[tuple[str, str], object] = {
    ("usage_session", "request_kind"): "unknown",
    ("usage_session", "project"): "",
    ("usage_session", "title"): "",
    # Snapshots taken before these columns existed carry no value for them.
    ("anthropic_keys", "role"): "primary",
    ("anthropic_keys", "allowed_proxy_keys"): "[]",
}

SNAPSHOT_IMPORT_ORDER: tuple[str, ...] = tuple(spec[0] for spec in SNAPSHOT_TABLE_SPECS)
SNAPSHOT_DELETE_ORDER: tuple[str, ...] = (
    "oauth_limit_wipe",
    "oauth_usage_snapshot",
    "oauth_window_usage",
    "oauth_window_usage_pending",
    "oauth_window_drop_log",
    "oauth_window_log",
    "anthropic_key_events",
    "anthropic_key_snapshots",
    "rate_limit_log",
    "usage_daily",
    "usage_kind_daily",
    "usage_session",
    "usage_key_hourly",
    "proxy_key_limits",
    "proxy_api_keys",
    "anthropic_keys",
    "model_prices",
)


WINDOW_USAGE_COUNTERS: tuple[str, ...] = (
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "cache_creation_5m_tokens",
    "cache_creation_1h_tokens", "web_search_requests", "requests",
)


def _window_counter_updates(table: str, backend: str) -> str:
    qualifier = f"{table}." if backend == "postgres" else ""
    return ", ".join(
        f"{c} = {qualifier}{c} + excluded.{c}" for c in WINDOW_USAGE_COUNTERS
    )


def build_window_usage_upsert_sql(backend: str) -> str:
    cols = ", ".join(WINDOW_USAGE_COUNTERS)
    return (
        f"INSERT INTO oauth_window_usage (window_id, model, {cols}) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        f"ON CONFLICT(window_id, model) DO UPDATE SET "
        f"{_window_counter_updates('oauth_window_usage', backend)}"
    )


def build_window_pending_upsert_sql(backend: str) -> str:
    cols = ", ".join(WINDOW_USAGE_COUNTERS)
    return (
        f"INSERT INTO oauth_window_usage_pending "
        f"(key_id, window_kind, model, {cols}, updated_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        f"ON CONFLICT(key_id, window_kind, model) DO UPDATE SET "
        f"{_window_counter_updates('oauth_window_usage_pending', backend)}, "
        f"updated_at = excluded.updated_at"
    )


def build_usage_upsert_sql(backend: str) -> str:
    base = """INSERT INTO usage_daily
                   (date, proxy_key, group_name, credential_id, provider, model,
                    via_openai_compat,
                    input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, cache_creation_5m_tokens,
                    cache_creation_1h_tokens, web_search_requests, requests)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, proxy_key, credential_id, provider, model, via_openai_compat)
               DO UPDATE SET
                   group_name            = COALESCE(excluded.group_name, usage_daily.group_name),
                   input_tokens          = {input_tokens} + excluded.input_tokens,
                   output_tokens         = {output_tokens} + excluded.output_tokens,
                   cache_read_tokens     = {cache_read_tokens} + excluded.cache_read_tokens,
                   cache_creation_tokens = {cache_creation_tokens} + excluded.cache_creation_tokens,
                   cache_creation_5m_tokens = {cache_creation_5m_tokens} + excluded.cache_creation_5m_tokens,
                   cache_creation_1h_tokens = {cache_creation_1h_tokens} + excluded.cache_creation_1h_tokens,
                   web_search_requests   = {web_search_requests} + excluded.web_search_requests,
                   requests              = {requests} + excluded.requests"""
    if backend == "postgres":
        qualifier = "usage_daily."
    else:
        qualifier = ""
    return base.format(
        input_tokens=f"{qualifier}input_tokens",
        output_tokens=f"{qualifier}output_tokens",
        cache_read_tokens=f"{qualifier}cache_read_tokens",
        cache_creation_tokens=f"{qualifier}cache_creation_tokens",
        cache_creation_5m_tokens=f"{qualifier}cache_creation_5m_tokens",
        cache_creation_1h_tokens=f"{qualifier}cache_creation_1h_tokens",
        web_search_requests=f"{qualifier}web_search_requests",
        requests=f"{qualifier}requests",
    )


_KIND_COUNTERS = (
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
    "cache_creation_5m_tokens", "cache_creation_1h_tokens", "web_search_requests", "requests",
)


def build_usage_kind_upsert_sql(backend: str) -> str:
    q = "usage_kind_daily." if backend == "postgres" else ""
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_kind_daily\n"
        "    (date, proxy_key, request_kind, provider, model,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(date, proxy_key, request_kind, provider, model) DO UPDATE SET\n"
        f"                   {sets}"
    )


def build_usage_session_upsert_sql(backend: str) -> str:
    q = "usage_session." if backend == "postgres" else ""
    # SQLite's MIN()/MAX() accept 2+ scalar args; Postgres only has the
    # aggregate forms, so the two-value comparison needs LEAST()/GREATEST().
    least_fn = "LEAST" if backend == "postgres" else "MIN"
    greatest_fn = "GREATEST" if backend == "postgres" else "MAX"
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_session\n"
        "    (session_id, proxy_key, request_kind, provider, model, first_date, last_date,\n"
        "     project, title,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(session_id, proxy_key, request_kind, provider, model) DO UPDATE SET\n"
        f"                   first_date = {least_fn}({q}first_date, excluded.first_date),\n"
        f"                   last_date  = {greatest_fn}({q}last_date, excluded.last_date),\n"
        f"                   project = CASE WHEN excluded.project <> '' THEN excluded.project ELSE {q}project END,\n"
        f"                   title   = CASE WHEN excluded.title   <> '' THEN excluded.title   ELSE {q}title END,\n"
        f"                   {sets}"
    )


def build_usage_hourly_upsert_sql(backend: str) -> str:
    q = "usage_key_hourly." if backend == "postgres" else ""
    sets = ",\n                   ".join(
        f"{c} = {q}{c} + excluded.{c}" for c in _KIND_COUNTERS)
    return (
        "INSERT INTO usage_key_hourly\n"
        "    (hour_utc, proxy_key, model,\n"
        "     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,\n"
        "     cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)\n"
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(hour_utc, proxy_key, model) DO UPDATE SET\n"
        f"                   {sets}"
    )


class DbUnavailable(Exception):
    """The database is unreachable and the operation was not attempted.

    Raised when the connection breaker is open, or when an operation died
    together with the connection. Deliberately NOT a ``psycopg.Error`` subclass,
    so "the database is down" can never be confused with "this SQL is wrong".

    Every best-effort call site already catches broad ``Exception`` and so keeps
    the proxy serving. Boot, migrations, CLI entry points and dashboard reads
    must let it propagate — an outage there should be loud, not silent.
    """


class Database:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None
        self._backend = "sqlite"

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA_SQL)
        await self._run_migrations()
        await self._seed_model_prices()
        await self._db.commit()
        logger.info("Database initialized at %s", self._path)

    async def _run_migrations(self) -> None:
        for sql in MIGRATIONS:
            try:
                await self.db.execute(sql)
            except Exception:
                pass
        await self._add_usage_via_openai_compat_column()
        await self._migrate_usage_session_add_kind_title()

    async def _add_usage_via_openai_compat_column(self) -> None:
        """Add usage_daily.via_openai_compat as a PRIMARY KEY member.

        SQLite cannot ALTER a PRIMARY KEY in place, so existing tables are
        rebuilt: historical rows are all native traffic and get 0. Fresh DBs
        already carry the column (via SCHEMA_SQL) and skip the rebuild.
        """
        cur = await self.db.execute("PRAGMA table_info(usage_daily)")
        old_columns = [row["name"] for row in await cur.fetchall()]
        if not old_columns or "via_openai_compat" in old_columns:
            return

        # Copy the columns both tables share; the new column defaults to 0.
        new_columns = [
            "date", "proxy_key", "group_name", "credential_id", "provider",
            "model", "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_creation_tokens", "cache_creation_5m_tokens",
            "cache_creation_1h_tokens", "web_search_requests", "requests",
        ]
        shared = [c for c in new_columns if c in old_columns]
        col_list = ", ".join(shared)

        savepoint = "add_usage_via_openai_compat"
        await self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            await self.db.execute("DROP TABLE IF EXISTS usage_daily_new")
            await self.db.execute(
                """CREATE TABLE usage_daily_new (
                    date                  TEXT    NOT NULL,
                    proxy_key             TEXT    NOT NULL DEFAULT '',
                    group_name            TEXT,
                    credential_id         TEXT    NOT NULL,
                    provider              TEXT    NOT NULL,
                    model                 TEXT    NOT NULL,
                    via_openai_compat     INTEGER NOT NULL DEFAULT 0,
                    input_tokens          INTEGER NOT NULL DEFAULT 0,
                    output_tokens         INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
                    web_search_requests   INTEGER NOT NULL DEFAULT 0,
                    requests              INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (date, proxy_key, credential_id, provider, model, via_openai_compat)
                )"""
            )
            await self.db.execute(
                f"INSERT INTO usage_daily_new ({col_list}) "
                f"SELECT {col_list} FROM usage_daily"
            )
            await self.db.execute("DROP TABLE usage_daily")
            await self.db.execute("ALTER TABLE usage_daily_new RENAME TO usage_daily")
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_daily_date ON usage_daily(date)"
            )
        except Exception:
            await self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            await self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            await self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        logger.info("Migrated usage_daily: added via_openai_compat column")

    async def _migrate_usage_session_add_kind_title(self) -> None:
        """Add request_kind (new PK member) + project/title to usage_session.

        SQLite cannot ALTER a PRIMARY KEY in place, so an existing table is
        rebuilt once; historical rows become request_kind='unknown',
        project='', title=''. Fresh DBs already carry the columns (SCHEMA_SQL)
        and skip. Idempotent — a no-op once request_kind exists.
        """
        cur = await self.db.execute("PRAGMA table_info(usage_session)")
        old_columns = [row["name"] for row in await cur.fetchall()]
        if not old_columns or "request_kind" in old_columns:
            return

        savepoint = "migrate_usage_session_kind_title"
        await self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            await self.db.execute("DROP TABLE IF EXISTS usage_session_new")
            await self.db.execute(
                """CREATE TABLE usage_session_new (
                    session_id   TEXT    NOT NULL,
                    proxy_key    TEXT    NOT NULL DEFAULT '',
                    request_kind TEXT    NOT NULL DEFAULT 'unknown',
                    provider     TEXT    NOT NULL,
                    model        TEXT    NOT NULL,
                    first_date   TEXT    NOT NULL,
                    last_date    TEXT    NOT NULL,
                    project      TEXT    NOT NULL DEFAULT '',
                    title        TEXT    NOT NULL DEFAULT '',
                    input_tokens             INTEGER NOT NULL DEFAULT 0,
                    output_tokens            INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
                    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
                    web_search_requests      INTEGER NOT NULL DEFAULT 0,
                    requests                 INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, proxy_key, request_kind, provider, model)
                )"""
            )
            await self.db.execute(
                "INSERT INTO usage_session_new "
                "(session_id, proxy_key, request_kind, provider, model, first_date, last_date, "
                " project, title, input_tokens, output_tokens, cache_read_tokens, "
                " cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, "
                " web_search_requests, requests) "
                "SELECT session_id, proxy_key, 'unknown', provider, model, first_date, last_date, "
                " '', '', input_tokens, output_tokens, cache_read_tokens, "
                " cache_creation_tokens, cache_creation_5m_tokens, cache_creation_1h_tokens, "
                " web_search_requests, requests FROM usage_session"
            )
            await self.db.execute("DROP TABLE usage_session")
            await self.db.execute("ALTER TABLE usage_session_new RENAME TO usage_session")
            await self.db.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_session_key ON usage_session(proxy_key)"
            )
        except Exception:
            await self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            await self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            await self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        logger.info("Migrated usage_session: added request_kind/project/title")

    async def _seed_model_prices(self) -> None:
        """Insert any default price rows not already present.

        Runs on every connect, not just for an empty table, so new default
        model prefixes (e.g. a newly released Claude model) reach databases
        seeded in the past. `seed_model_prices` uses INSERT OR IGNORE /
        ON CONFLICT DO NOTHING, so it never touches a prefix that's already
        present, including one a user customized via `price set`.
        """
        from smart_proxy.usage import build_default_price_rows

        await self.seed_model_prices(build_default_price_rows())

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @asynccontextmanager
    async def transaction(self):
        """Group statements so they commit or roll back together.

        On sqlite this is today's behaviour made explicit: statements, then one
        commit. The Postgres backend overrides it with a real transaction on the
        shared connection, serialised so concurrent tasks cannot join each
        other's.
        """
        yield self
        await self.db.commit()

    def is_available(self) -> bool:
        """Whether the backend is usable right now.

        Always true for sqlite (a local file with no connection to lose); the
        Postgres backend overrides this with its breaker state. Callers use it to
        decide whether an operation is worth attempting at all — notably the
        OAuth refresh, which must not consume a single-use token it cannot
        persist.
        """
        return True

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    def _encode_snapshot_value(
        self,
        table: str,
        column: str,
        value,  # noqa: ANN001
    ):
        if value is None:
            default = _SNAPSHOT_COLUMN_DEFAULTS.get((table, column))
            if default is not None:
                return default
        return value

    def _build_select_columns(self, table: str, columns: tuple[str, ...]) -> str:
        del table
        return ", ".join(columns)

    def _build_insert_columns(self, table: str, columns: tuple[str, ...]) -> str:
        del table
        return ", ".join(columns)

    async def _export_table_rows(
        self,
        table: str,
        columns: tuple[str, ...],
        order_by: str,
    ) -> list[dict]:
        selected_columns = self._build_select_columns(table, columns)
        cur = await self.db.execute(
            f"SELECT {selected_columns} FROM {table} ORDER BY {order_by}"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def _replace_table_rows(
        self,
        table: str,
        columns: tuple[str, ...],
        rows: list[dict],
    ) -> None:
        if not rows:
            return
        placeholders = ", ".join("?" for _ in columns)
        selected_columns = self._build_insert_columns(table, columns)
        payload = [
            tuple(self._encode_snapshot_value(table, column, row.get(column)) for column in columns)
            for row in rows
        ]
        await self.db.executemany(
            f"INSERT INTO {table} ({selected_columns}) VALUES ({placeholders})",
            payload,
        )

    async def _after_replace_snapshot(self, snapshot: dict[str, list[dict]]) -> None:
        del snapshot  # hook for backend-specific sequence reset

    # ------------------------------------------------------------------
    # Proxy API keys
    # ------------------------------------------------------------------

    async def add_proxy_key(self, key: str, name: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "INSERT INTO proxy_api_keys (key, name, active, created_at) VALUES (?, ?, 1, ?)",
            (key, name, now),
        )
        await self.db.commit()

    async def list_proxy_keys(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT key, name, active, created_at FROM proxy_api_keys ORDER BY created_at DESC"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def revoke_proxy_key(self, key_prefix: str) -> str | None:
        """Deactivate a proxy key matching the prefix. Returns the full key or None."""
        cur = await self.db.execute(
            "SELECT key FROM proxy_api_keys WHERE LOWER(key) LIKE LOWER(?) AND active = 1",
            (key_prefix + "%",),
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        full_key = rows[0]["key"]
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE proxy_api_keys SET active = 0 WHERE key = ?",
            (full_key,),
        )
        await self.db.commit()
        return full_key

    async def set_proxy_key_active(self, key_prefix: str, active: bool) -> str | None:
        """Enable/disable a proxy key matching the prefix. Returns the full key
        or None when zero or multiple keys match."""
        cur = await self.db.execute(
            "SELECT key FROM proxy_api_keys WHERE LOWER(key) LIKE LOWER(?)",
            (key_prefix + "%",),
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        full_key = rows[0]["key"]
        await self.db.execute(
            "UPDATE proxy_api_keys SET active = ? WHERE key = ?",
            (1 if active else 0, full_key),
        )
        await self.db.commit()
        return full_key

    async def set_proxy_key_active_by_created_at(
        self, created_at: str, active: bool
    ) -> str | None:
        """Enable/disable the proxy key with this exact ``created_at``. Returns
        the full key, or None when zero or multiple keys match. ``created_at``
        is a stable per-row identity; the display prefix is not — distinct keys
        can share leading characters (observed: two keys differing only in the
        final character), which makes prefix matching ambiguous."""
        cur = await self.db.execute(
            "SELECT key FROM proxy_api_keys WHERE created_at = ?",
            (created_at,),
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        full_key = rows[0]["key"]
        await self.db.execute(
            "UPDATE proxy_api_keys SET active = ? WHERE key = ?",
            (1 if active else 0, full_key),
        )
        await self.db.commit()
        return full_key

    async def get_active_proxy_key_names(self) -> dict[str, str]:
        """Active proxy keys mapped to their human name.

        The proxy holds this so a failure can say *which consumer* was hit
        without a per-request query -- "Acme (webapp)" is actionable where a
        key prefix is not.
        """
        cur = await self.db.execute(
            "SELECT key, name FROM proxy_api_keys WHERE active = 1"
        )
        return {row["key"]: (row["name"] or "") for row in await cur.fetchall()}

    async def get_proxy_key_by_created_at(self, created_at: str) -> str | None:
        """Full proxy key with this exact ``created_at``, or None when zero or
        multiple rows match. ``created_at`` is the stable per-row identity; the
        display prefix is not (distinct keys can share leading characters)."""
        cur = await self.db.execute(
            "SELECT key FROM proxy_api_keys WHERE created_at = ?", (created_at,)
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        return str(rows[0]["key"])

    # ------------------------------------------------------------------
    # Proxy key spend limits
    # ------------------------------------------------------------------

    async def list_proxy_key_limits(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT proxy_key, kind, amount FROM proxy_key_limits"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def set_proxy_key_limit(
        self, proxy_key: str, kind: str, amount: float
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """INSERT INTO proxy_key_limits (proxy_key, kind, amount, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(proxy_key, kind) DO UPDATE SET
                   amount = excluded.amount,
                   updated_at = excluded.updated_at""",
            (proxy_key, kind, float(amount), now),
        )
        await self.db.commit()

    async def delete_proxy_key_limit(self, proxy_key: str, kind: str) -> None:
        await self.db.execute(
            "DELETE FROM proxy_key_limits WHERE proxy_key = ? AND kind = ?",
            (proxy_key, kind),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    async def upsert_usage_batch(self, rows: list[tuple]) -> None:
        """Batch-upsert daily usage rows.

        Each tuple: (date, proxy_key, group_name, credential_id, provider, model,
                      via_openai_compat,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_creation_tokens, cache_creation_5m_tokens,
                      cache_creation_1h_tokens, web_search_requests, requests)
        """
        async with self.transaction():
            if not rows:
                return
            await self.db.executemany(
                build_usage_upsert_sql(self._backend),
                rows,
            )
            await self.db.commit()

    async def upsert_usage_kind_batch(self, rows: list[tuple]) -> None:
        """Batch-upsert per-request-kind daily usage rows.

        Each tuple: (date, proxy_key, request_kind, provider, model,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_creation_tokens, cache_creation_5m_tokens,
                      cache_creation_1h_tokens, web_search_requests, requests)
        """
        async with self.transaction():
            if not rows:
                return
            await self.db.executemany(build_usage_kind_upsert_sql(self._backend), rows)
            await self.db.commit()

    async def upsert_usage_session_batch(self, rows: list[tuple]) -> None:
        """Batch-upsert per-session usage rows.

        Each tuple (canonical 17-col order):
          (session_id, proxy_key, request_kind, provider, model,
           first_date, last_date, project, title,
           input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
           cache_creation_5m_tokens, cache_creation_1h_tokens, web_search_requests, requests)
        """
        async with self.transaction():
            if not rows:
                return
            await self.db.executemany(build_usage_session_upsert_sql(self._backend), rows)
            await self.db.commit()

    async def upsert_usage_hourly_batch(self, rows: list[tuple]) -> None:
        """Batch-upsert hourly per-key usage buckets (spend-limit source).

        Each tuple: (hour_utc, proxy_key, model,
                      input_tokens, output_tokens, cache_read_tokens,
                      cache_creation_tokens, cache_creation_5m_tokens,
                      cache_creation_1h_tokens, web_search_requests, requests)
        """
        async with self.transaction():
            if not rows:
                return
            await self.db.executemany(build_usage_hourly_upsert_sql(self._backend), rows)
            await self.db.commit()

    async def query_usage_key_hourly(
        self, start_hour: str, end_hour: str
    ) -> list[dict]:
        """Token sums per (proxy_key, model) over [start_hour, end_hour).

        Hours are ``'%Y-%m-%dT%H'`` strings, which sort lexicographically in
        chronological order, so a plain string range works on both backends.
        """
        cur = await self.db.execute(
            """SELECT proxy_key, model,
                      SUM(input_tokens)  AS input_tokens,
                      SUM(output_tokens) AS output_tokens,
                      SUM(cache_read_tokens) AS cache_read_tokens,
                      SUM(cache_creation_tokens) AS cache_creation_tokens,
                      SUM(cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                      SUM(cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                      SUM(web_search_requests) AS web_search_requests,
                      SUM(requests) AS requests
               FROM usage_key_hourly
               WHERE hour_utc >= ? AND hour_utc < ?
               GROUP BY proxy_key, model""",
            (start_hour, end_hour),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def prune_usage_key_hourly(self, before_hour: str) -> None:
        """Drop hourly buckets older than ``before_hour`` (retention)."""
        await self.db.execute(
            "DELETE FROM usage_key_hourly WHERE hour_utc < ?", (before_hour,)
        )
        await self.db.commit()

    async def query_usage(
        self,
        start_date: str,
        end_date: str,
        *,
        by_key: bool = False,
    ) -> list[dict]:
        """Return daily usage for a date range.

        When *by_key* is True, results include proxy_key and key name columns
        and are grouped per proxy key.
        """
        if by_key:
            cur = await self.db.execute(
                """SELECT u.date, u.proxy_key, u.group_name,
                          COALESCE(pk.name, '') AS key_name,
                          u.provider, u.model, u.via_openai_compat,
                          SUM(u.input_tokens)  AS input_tokens,
                          SUM(u.output_tokens) AS output_tokens,
                          SUM(u.cache_read_tokens) AS cache_read_tokens,
                          SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                          SUM(u.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                          SUM(u.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                          SUM(u.web_search_requests) AS web_search_requests,
                          SUM(u.requests)      AS requests
                   FROM usage_daily u
                   LEFT JOIN proxy_api_keys pk ON pk.key = u.proxy_key
                   WHERE u.date >= ? AND u.date <= ?
                   GROUP BY u.date, u.proxy_key, u.group_name, pk.name, u.provider, u.model,
                            u.via_openai_compat
                   ORDER BY u.date, u.group_name, key_name, u.provider, u.model""",
                (start_date, end_date),
            )
        else:
            cur = await self.db.execute(
                """SELECT date, provider, model,
                          SUM(input_tokens)  AS input_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(cache_read_tokens) AS cache_read_tokens,
                          SUM(cache_creation_tokens) AS cache_creation_tokens,
                          SUM(cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                          SUM(cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                          SUM(web_search_requests) AS web_search_requests,
                          SUM(requests)      AS requests
                   FROM usage_daily
                   WHERE date >= ? AND date <= ?
                   GROUP BY date, provider, model
                   ORDER BY date, provider, model""",
                (start_date, end_date),
            )
        return [dict(row) for row in await cur.fetchall()]

    async def query_usage_by_key_model(
        self,
        start_date: str,
        end_date: str,
    ) -> list[dict]:
        """Return usage aggregated by proxy key, group, provider, and model."""
        cur = await self.db.execute(
            """SELECT u.proxy_key, u.group_name,
                      COALESCE(pk.name, '') AS key_name,
                      u.provider, u.model, u.via_openai_compat,
                      SUM(u.input_tokens)  AS input_tokens,
                      SUM(u.output_tokens) AS output_tokens,
                      SUM(u.cache_read_tokens) AS cache_read_tokens,
                      SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                      SUM(u.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                      SUM(u.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                      SUM(u.web_search_requests) AS web_search_requests,
                      SUM(u.requests)      AS requests
               FROM usage_daily u
               LEFT JOIN proxy_api_keys pk ON pk.key = u.proxy_key
               WHERE u.date >= ? AND u.date <= ?
               GROUP BY u.proxy_key, u.group_name, pk.name, u.provider, u.model,
                        u.via_openai_compat
               ORDER BY COALESCE(NULLIF(pk.name, ''), NULLIF(u.group_name, ''), u.proxy_key),
                        u.provider, u.model""",
            (start_date, end_date),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def query_usage_by_kind(self, start_date: str, end_date: str) -> list[dict]:
        """Return usage aggregated by request kind (main/subagent/etc), joined to key name."""
        cur = await self.db.execute(
            """SELECT u.proxy_key, COALESCE(pk.name, '') AS key_name, u.request_kind,
                      u.provider, u.model,
                      SUM(u.input_tokens) AS input_tokens,
                      SUM(u.output_tokens) AS output_tokens,
                      SUM(u.cache_read_tokens) AS cache_read_tokens,
                      SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                      SUM(u.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                      SUM(u.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                      SUM(u.web_search_requests) AS web_search_requests,
                      SUM(u.requests) AS requests
               FROM usage_kind_daily u
               LEFT JOIN proxy_api_keys pk ON pk.key = u.proxy_key
               WHERE u.date >= ? AND u.date <= ?
               GROUP BY u.proxy_key, pk.name, u.request_kind, u.provider, u.model""",
            (start_date, end_date),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def query_top_sessions(self, limit: int = 50) -> list[dict]:
        """Return the top sessions by total token volume, joined to key name.

        Ranks *whole sessions* (by their total token volume across all
        models) in a grouped subquery, applies LIMIT to that session-level
        ranking, then joins back to return every per-(request_kind, provider,
        model) row for each of those sessions (each carrying its project/title
        label). This avoids the bug where applying LIMIT at the
        per-(session, proxy_key, provider, model) row grain could silently
        drop a multi-model session's smaller-model row — truncating its
        reported total and potentially excluding it from the top-N entirely
        even though its combined total belonged there. Callers (notably
        ``build_sessions_json``) sum the per-(kind, model) rows back into one
        entry per session with a per-request_kind breakdown.

        Uses a JOIN to a grouped subquery rather than a row-value
        ``(session_id, proxy_key) IN (...)`` predicate, since row-value
        `IN` isn't portable across sqlite and Postgres.
        """
        cur = await self.db.execute(
            """SELECT s.session_id, s.proxy_key, COALESCE(pk.name, '') AS key_name,
                      s.request_kind,
                      s.provider, s.model,
                      MAX(s.project) AS project, MAX(s.title) AS title,
                      MIN(s.first_date) AS first_date,
                      MAX(s.last_date) AS last_date,
                      SUM(s.input_tokens) AS input_tokens,
                      SUM(s.output_tokens) AS output_tokens,
                      SUM(s.cache_read_tokens) AS cache_read_tokens,
                      SUM(s.cache_creation_tokens) AS cache_creation_tokens,
                      SUM(s.cache_creation_5m_tokens) AS cache_creation_5m_tokens,
                      SUM(s.cache_creation_1h_tokens) AS cache_creation_1h_tokens,
                      SUM(s.web_search_requests) AS web_search_requests,
                      SUM(s.requests) AS requests
               FROM usage_session s
               JOIN (
                   SELECT session_id, proxy_key,
                          SUM(input_tokens + output_tokens + cache_read_tokens
                              + cache_creation_tokens) AS total_tokens
                   FROM usage_session
                   GROUP BY session_id, proxy_key
                   ORDER BY total_tokens DESC
                   LIMIT ?
               ) top ON top.session_id = s.session_id AND top.proxy_key = s.proxy_key
               LEFT JOIN proxy_api_keys pk ON pk.key = s.proxy_key
               GROUP BY s.session_id, s.proxy_key, pk.name, s.request_kind,
                        s.provider, s.model, top.total_tokens
               ORDER BY top.total_tokens DESC""",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    # ------------------------------------------------------------------
    # Model prices
    # ------------------------------------------------------------------

    async def count_model_prices(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) as cnt FROM model_prices")
        row = await cur.fetchone()
        return int(row["cnt"]) if row else 0

    async def get_all_model_prices(self) -> list[dict]:
        cur = await self.db.execute(
            """SELECT model_prefix, provider, input_price, output_price,
                      cache_read_price, cache_write_5m_price, cache_write_1h_price,
                      updated_at
               FROM model_prices
               ORDER BY LENGTH(model_prefix) DESC, model_prefix ASC"""
        )
        return [dict(row) for row in await cur.fetchall()]

    async def upsert_model_price(
        self,
        *,
        model_prefix: str,
        provider: str,
        input_price: float,
        output_price: float,
        cache_read_price: float | None = None,
        cache_write_5m_price: float | None = None,
        cache_write_1h_price: float | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """INSERT INTO model_prices
               (model_prefix, provider, input_price, output_price,
                cache_read_price, cache_write_5m_price, cache_write_1h_price, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_prefix) DO UPDATE SET
                   provider = excluded.provider,
                   input_price = excluded.input_price,
                   output_price = excluded.output_price,
                   cache_read_price = excluded.cache_read_price,
                   cache_write_5m_price = excluded.cache_write_5m_price,
                   cache_write_1h_price = excluded.cache_write_1h_price,
                   updated_at = excluded.updated_at""",
            (
                model_prefix,
                provider,
                input_price,
                output_price,
                cache_read_price,
                cache_write_5m_price,
                cache_write_1h_price,
                now,
            ),
        )
        await self.db.commit()

    async def seed_model_prices(self, rows: list[tuple]) -> int:
        """Insert default model prices while preserving existing custom rows."""
        if not rows:
            return 0
        before = await self.count_model_prices()
        now = datetime.now(timezone.utc).isoformat()
        payload = [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                now,
            )
            for row in rows
        ]
        sql = """INSERT OR IGNORE INTO model_prices
               (model_prefix, provider, input_price, output_price,
                cache_read_price, cache_write_5m_price, cache_write_1h_price, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""
        if self._backend == "postgres":
            sql = """INSERT INTO model_prices
               (model_prefix, provider, input_price, output_price,
                cache_read_price, cache_write_5m_price, cache_write_1h_price, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_prefix) DO NOTHING"""
        await self.db.executemany(sql, payload)
        await self.db.commit()
        after = await self.count_model_prices()
        return max(0, after - before)

    # ------------------------------------------------------------------
    # Anthropic keys
    # ------------------------------------------------------------------

    async def record_anthropic_key_snapshot(
        self,
        *,
        key_id: str,
        snapshot_kind: str,
        trigger_event_type: str,
        row: dict | None = None,
        commit: bool = True,
    ) -> int | None:
        snapshot_row = row or await self.get_anthropic_key(key_id)
        if snapshot_row is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        row_json = json.dumps(snapshot_row, ensure_ascii=False)
        cur = await self.db.execute(
            """INSERT INTO anthropic_key_snapshots
               (key_id, snapshot_kind, trigger_event_type, status, key_type, name,
                client_id, expires_at, access_token, refresh_token, scopes,
                subscription_type, rate_limit_tier, row_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                key_id,
                snapshot_kind,
                trigger_event_type,
                snapshot_row.get("status", ""),
                snapshot_row.get("key_type", ""),
                snapshot_row.get("name", ""),
                snapshot_row.get("client_id", ""),
                snapshot_row.get("expires_at"),
                snapshot_row.get("access_token"),
                snapshot_row.get("refresh_token"),
                snapshot_row.get("scopes", "[]"),
                snapshot_row.get("subscription_type", ""),
                snapshot_row.get("rate_limit_tier", ""),
                row_json,
                now,
            ),
        )
        if commit:
            await self.db.commit()
        lastrowid = getattr(cur, "lastrowid", None)
        if lastrowid is not None:
            return int(lastrowid)
        cur = await self.db.execute(
            "SELECT id FROM anthropic_key_snapshots WHERE key_id = ? ORDER BY id DESC LIMIT 1",
            (key_id,),
        )
        row = await cur.fetchone()
        return int(row["id"]) if row else None

    async def record_anthropic_key_event(
        self,
        *,
        key_id: str,
        event_type: str,
        op_id: str = "",
        source: str = "",
        decision: str = "",
        path: str = "",
        model: str | None = None,
        http_status: int | None = None,
        request_id: str = "",
        error_type: str = "",
        error_message: str = "",
        retry_after: int | None = None,
        snapshot_id: int | None = None,
        context: dict | None = None,
        commit: bool = True,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = await self.db.execute(
            """INSERT INTO anthropic_key_events
               (op_id, key_id, source, event_type, decision, path, model,
                http_status, request_id, error_type, error_message, retry_after,
                snapshot_id, context_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                op_id,
                key_id,
                source,
                event_type,
                decision,
                path,
                model,
                http_status,
                request_id,
                error_type,
                error_message,
                retry_after,
                snapshot_id,
                json.dumps(context or {}, ensure_ascii=False),
                now,
            ),
        )
        if commit:
            await self.db.commit()
        lastrowid = getattr(cur, "lastrowid", None)
        if lastrowid is not None:
            return int(lastrowid)
        cur = await self.db.execute(
            "SELECT id FROM anthropic_key_events WHERE key_id = ? ORDER BY id DESC LIMIT 1",
            (key_id,),
        )
        row = await cur.fetchone()
        return int(row["id"]) if row else 0

    async def list_anthropic_key_snapshots(self, key_id: str | None = None) -> list[dict]:
        if key_id:
            cur = await self.db.execute(
                "SELECT * FROM anthropic_key_snapshots WHERE key_id = ? ORDER BY id",
                (key_id,),
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM anthropic_key_snapshots ORDER BY id"
            )
        return [dict(row) for row in await cur.fetchall()]

    async def list_anthropic_key_events(self, key_id: str | None = None) -> list[dict]:
        if key_id:
            cur = await self.db.execute(
                "SELECT * FROM anthropic_key_events WHERE key_id = ? ORDER BY id",
                (key_id,),
            )
        else:
            cur = await self.db.execute(
                "SELECT * FROM anthropic_key_events ORDER BY id"
            )
        return [dict(row) for row in await cur.fetchall()]

    async def insert_anthropic_key(
        self,
        *,
        id: str,
        key_type: str,
        api_key: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        client_id: str = "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        expires_at: int | None = None,
        scopes: str = "[]",
        subscription_type: str = "",
        rate_limit_tier: str = "",
        name: str = "",
        role: str = "primary",
        allowed_proxy_keys: list[str] | None = None,
    ) -> None:
        """Create a key. ``role`` and ``allowed_proxy_keys`` are set in the same
        INSERT so a paid fallback key never exists as an unscoped primary, not
        even for the moment between two statements."""
        async with self.transaction():
            now = datetime.now(timezone.utc).isoformat()
            scope_json = json.dumps(sorted({k.strip() for k in (allowed_proxy_keys or []) if k.strip()}))
            await self.db.execute(
                """INSERT INTO anthropic_keys
                   (id, key_type, status, api_key, access_token, refresh_token,
                    client_id, expires_at, scopes, subscription_type,
                    rate_limit_tier, name, role, allowed_proxy_keys, created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id, key_type, api_key, access_token, refresh_token,
                    client_id, expires_at, scopes, subscription_type,
                    rate_limit_tier, name, role, scope_json, now, now,
                ),
            )
            await self.db.commit()

    async def get_active_anthropic_keys(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM anthropic_keys WHERE status IN ('active', 'low_balance') ORDER BY created_at"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def get_low_balance_anthropic_keys(self) -> list[dict]:
        cur = await self.db.execute(
            """SELECT id, key_type, api_key, access_token, refresh_token
               FROM anthropic_keys
               WHERE status = 'low_balance'
               ORDER BY created_at"""
        )
        return [dict(row) for row in await cur.fetchall()]

    async def get_active_anthropic_oauth_keys(self) -> list[dict]:
        cur = await self.db.execute(
            """SELECT * FROM anthropic_keys
               WHERE status = 'active' AND key_type = 'oauth'
               ORDER BY created_at"""
        )
        return [dict(row) for row in await cur.fetchall()]

    async def get_anthropic_key(self, key_id: str) -> dict | None:
        cur = await self.db.execute(
            "SELECT * FROM anthropic_keys WHERE id = ?", (key_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def update_anthropic_oauth_tokens(
        self,
        key_id: str,
        access_token: str,
        expires_at: int,
        refresh_token: str | None = None,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_event_type: str = "",
        audit_decision: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        audit_http_status: int | None = None,
        audit_request_id: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_retry_after: int | None = None,
        audit_context: dict | None = None,
    ) -> None:
        # Deliberately NOT one transaction. Under autocommit each statement stands
        # on its own, which is what durability needs here: the rotated token is the
        # only living one, and an audit row that fails to insert must never take it
        # down with it. Atomicity would trade a permanent key loss for a tidy log.
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id: int | None = None
        if audit_event_type:
            snapshot_id = await self.record_anthropic_key_snapshot(
                key_id=key_id,
                snapshot_kind="before_refresh_update",
                trigger_event_type=audit_event_type,
                commit=False,
            )
        if refresh_token:
            await self.db.execute(
                """UPDATE anthropic_keys
                   SET access_token = ?, refresh_token = ?, expires_at = ?, updated_at = ?
                   WHERE id = ?""",
                (access_token, refresh_token, expires_at, now, key_id),
            )
        else:
            await self.db.execute(
                """UPDATE anthropic_keys
                   SET access_token = ?, expires_at = ?, updated_at = ?
                   WHERE id = ?""",
                (access_token, expires_at, now, key_id),
            )
        if audit_event_type:
            await self.record_anthropic_key_event(
                key_id=key_id,
                event_type=audit_event_type,
                op_id=audit_op_id,
                source=audit_source,
                decision=audit_decision,
                path=audit_path,
                model=audit_model,
                http_status=audit_http_status,
                request_id=audit_request_id,
                error_type=audit_error_type,
                error_message=audit_error_message,
                retry_after=audit_retry_after,
                snapshot_id=snapshot_id,
                context=audit_context,
                commit=False,
            )
        await self.db.commit()

    async def deactivate_anthropic_key(self, key_id: str) -> None:
        await self.set_anthropic_key_status(key_id, "inactive")

    async def set_anthropic_key_status(
        self,
        key_id: str,
        status: str,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_event_type: str = "",
        audit_decision: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        audit_http_status: int | None = None,
        audit_request_id: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_retry_after: int | None = None,
        audit_context: dict | None = None,
    ) -> None:
        # Not one transaction, for the same reason as update_anthropic_oauth_tokens:
        # the status change is the fact, the audit row only describes it.
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id: int | None = None
        previous = await self.get_anthropic_key(key_id) if audit_event_type else None
        if previous is not None:
            snapshot_id = await self.record_anthropic_key_snapshot(
                key_id=key_id,
                snapshot_kind="before_status_change",
                trigger_event_type=audit_event_type,
                row=previous,
                commit=False,
            )
        await self.db.execute(
            "UPDATE anthropic_keys SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, key_id),
        )
        if audit_event_type:
            context = dict(audit_context or {})
            if previous is not None:
                context.setdefault("previous_status", previous.get("status", ""))
            context.setdefault("next_status", status)
            await self.record_anthropic_key_event(
                key_id=key_id,
                event_type=audit_event_type,
                op_id=audit_op_id,
                source=audit_source,
                decision=audit_decision,
                path=audit_path,
                model=audit_model,
                http_status=audit_http_status,
                request_id=audit_request_id,
                error_type=audit_error_type,
                error_message=audit_error_message,
                retry_after=audit_retry_after,
                snapshot_id=snapshot_id,
                context=context,
                commit=False,
            )
        await self.db.commit()

    async def set_anthropic_key_name(
        self,
        key_id: str,
        name: str,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_event_type: str = "",
        audit_decision: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        audit_http_status: int | None = None,
        audit_request_id: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_retry_after: int | None = None,
        audit_context: dict | None = None,
    ) -> bool:
        """Rename an Anthropic key. Returns False when the id does not exist."""
        previous = await self.get_anthropic_key(key_id)
        if previous is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id: int | None = None
        if audit_event_type:
            snapshot_id = await self.record_anthropic_key_snapshot(
                key_id=key_id,
                snapshot_kind="before_rename",
                trigger_event_type=audit_event_type,
                row=previous,
                commit=False,
            )
        await self.db.execute(
            "UPDATE anthropic_keys SET name = ?, updated_at = ? WHERE id = ?",
            (name, now, key_id),
        )
        if audit_event_type:
            context = dict(audit_context or {})
            context.setdefault("previous_name", previous.get("name", ""))
            context.setdefault("next_name", name)
            await self.record_anthropic_key_event(
                key_id=key_id,
                event_type=audit_event_type,
                op_id=audit_op_id,
                source=audit_source,
                decision=audit_decision,
                path=audit_path,
                model=audit_model,
                http_status=audit_http_status,
                request_id=audit_request_id,
                error_type=audit_error_type,
                error_message=audit_error_message,
                retry_after=audit_retry_after,
                snapshot_id=snapshot_id,
                context=context,
                commit=False,
            )
        await self.db.commit()
        return True

    async def set_anthropic_key_role(
        self,
        key_id: str,
        role: str,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_event_type: str = "",
        audit_decision: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        audit_http_status: int | None = None,
        audit_request_id: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_retry_after: int | None = None,
        audit_context: dict | None = None,
    ) -> bool:
        """Set an Anthropic key's role ('primary'|'standby'). Returns False if id missing."""
        # Not one transaction — the role change must survive a failed audit row.
        previous = await self.get_anthropic_key(key_id)
        if previous is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id: int | None = None
        if audit_event_type:
            snapshot_id = await self.record_anthropic_key_snapshot(
                key_id=key_id, snapshot_kind="before_role_change",
                trigger_event_type=audit_event_type, row=previous, commit=False,
            )
        await self.db.execute(
            "UPDATE anthropic_keys SET role = ?, updated_at = ? WHERE id = ?",
            (role, now, key_id),
        )
        if audit_event_type:
            context = dict(audit_context or {})
            context.setdefault("previous_role", previous.get("role", "primary"))
            context.setdefault("next_role", role)
            await self.record_anthropic_key_event(
                key_id=key_id, event_type=audit_event_type, op_id=audit_op_id,
                source=audit_source, decision=audit_decision, path=audit_path,
                model=audit_model, http_status=audit_http_status, request_id=audit_request_id,
                error_type=audit_error_type, error_message=audit_error_message,
                retry_after=audit_retry_after, snapshot_id=snapshot_id, context=context, commit=False,
            )
        await self.db.commit()
        return True

    async def set_anthropic_key_scope(
        self,
        key_id: str,
        proxy_keys: list[str],
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_event_type: str = "",
        audit_decision: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_context: dict | None = None,
    ) -> bool:
        """Replace the set of proxy keys allowed to escalate onto this key.

        ``proxy_keys`` holds *full* ``sp-*`` keys (the request presents the full
        bearer token, and the displayed 12-char prefix is not unique). The audit
        context records only masked prefixes — never key material.
        """
        previous = await self.get_anthropic_key(key_id)
        if previous is None:
            return False
        cleaned = sorted({k.strip() for k in proxy_keys if k and k.strip()})
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id: int | None = None
        if audit_event_type:
            snapshot_id = await self.record_anthropic_key_snapshot(
                key_id=key_id, snapshot_kind="before_scope_change",
                trigger_event_type=audit_event_type, row=previous, commit=False,
            )
        await self.db.execute(
            "UPDATE anthropic_keys SET allowed_proxy_keys = ?, updated_at = ? WHERE id = ?",
            (json.dumps(cleaned), now, key_id),
        )
        if audit_event_type:
            context = dict(audit_context or {})
            context.setdefault(
                "previous_scope",
                [k[:12] for k in parse_allowed_proxy_keys(previous.get("allowed_proxy_keys"))],
            )
            context.setdefault("next_scope", [k[:12] for k in cleaned])
            await self.record_anthropic_key_event(
                key_id=key_id, event_type=audit_event_type, op_id=audit_op_id,
                source=audit_source, decision=audit_decision,
                error_type=audit_error_type, error_message=audit_error_message,
                snapshot_id=snapshot_id, context=context, commit=False,
            )
        await self.db.commit()
        return True

    async def list_anthropic_keys(self) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM anthropic_keys ORDER BY created_at DESC"
        )
        return [dict(row) for row in await cur.fetchall()]

    async def deactivate_anthropic_key_by_prefix(
        self,
        prefix: str,
        **audit_kwargs,
    ) -> str | None:
        cur = await self.db.execute(
            "SELECT id FROM anthropic_keys WHERE LOWER(id) LIKE LOWER(?) AND status = 'active'",
            (prefix + "%",),
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        full_id = rows[0]["id"]
        await self.set_anthropic_key_status(full_id, "inactive", **audit_kwargs)
        return full_id

    async def activate_anthropic_key_by_prefix(
        self,
        prefix: str,
        **audit_kwargs,
    ) -> str | None:
        cur = await self.db.execute(
            "SELECT id FROM anthropic_keys WHERE LOWER(id) LIKE LOWER(?)",
            (prefix + "%",),
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            return None
        full_id = rows[0]["id"]
        await self.set_anthropic_key_status(full_id, "active", **audit_kwargs)
        return full_id

    async def record_rate_limit(
        self,
        provider: str,
        credential_id: str,
        retry_after: int | None,
        reset_at: str | None,
        limit_type: str,
        utilization_5h: float | None,
        utilization_7d: float | None,
    ) -> None:
        """Insert or bump an active rate-limit event (dedup by credential + period)."""
        now = datetime.now(timezone.utc).isoformat()
        if reset_at:
            cur = await self.db.execute(
                """SELECT id FROM rate_limit_log
                   WHERE credential_id = ? AND reset_at = ? AND provider = ?""",
                (credential_id, reset_at, provider),
            )
            existing = await cur.fetchone()
            if existing:
                await self.db.execute(
                    """UPDATE rate_limit_log
                       SET requests_during = requests_during + 1,
                           utilization_5h = COALESCE(?, utilization_5h),
                           utilization_7d = COALESCE(?, utilization_7d)
                       WHERE id = ?""",
                    (utilization_5h, utilization_7d, existing["id"]),
                )
                await self.db.commit()
                return
        await self.db.execute(
            """INSERT INTO rate_limit_log
               (provider, credential_id, started_at, retry_after, reset_at,
                limit_type, utilization_5h, utilization_7d)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (provider, credential_id, now, retry_after, reset_at,
             limit_type, utilization_5h, utilization_7d),
        )
        await self.db.commit()

    async def record_oauth_window_observations(
        self,
        key_id: str,
        observations: list[dict],
        *,
        seen_at: str | None = None,
    ) -> list[dict]:
        """Upsert one row per rate-limit window instance; return reset events.

        Each observation: ``{"window_kind", "resets_at" (minute-truncated ISO,
        window identity), "resets_at_raw", "utilization" (percent or None)}``.
        Two event types are returned for the caller to log:

        - ``window_reset`` — a new row for a kind that already has an older
          row, i.e. the API declared a new window (``resets_at`` moved by
          more than ``RESETS_AT_JITTER_TOLERANCE_MINUTES``);
        - ``utilization_drop`` — utilization fell by
          ``UTILIZATION_DROP_THRESHOLD_PP`` or more in a single step while
          ``resets_at`` stayed the same: the window reset *earlier* than the
          API claimed.
        """
        async with self.transaction():
            now = seen_at or datetime.now(timezone.utc).isoformat()
            reset_events: list[dict] = []
            for obs in observations:
                kind = obs["window_kind"]
                resets_at = obs["resets_at"]
                utilization = obs.get("utilization")
                cur = await self.db.execute(
                    """SELECT id, resets_at, first_active_at, last_seen_at,
                              last_utilization, max_utilization, max_utilization_at
                       FROM oauth_window_log
                       WHERE key_id = ? AND window_kind = ?
                       ORDER BY resets_at DESC LIMIT 1""",
                    (key_id, kind),
                )
                latest = await cur.fetchone()
                existing = None
                if latest is not None:
                    drift = _iso_minutes_between(latest["resets_at"], resets_at)
                    if drift is not None and abs(drift) <= RESETS_AT_JITTER_TOLERANCE_MINUTES:
                        existing = latest
                if existing:
                    prev_last = existing["last_utilization"]
                    # A weekly counter reaching exactly zero is recorded whatever
                    # its size, so the drop log is a complete journal of wipes:
                    # it is what reconciliation reads to recover a wipe the
                    # in-memory detector missed, and a 3%-to-0 wipe is still a
                    # wipe. Other kinds keep the 5 pp threshold.
                    if (
                        utilization is not None
                        and prev_last is not None
                        and (
                            prev_last - utilization >= UTILIZATION_DROP_THRESHOLD_PP
                            or (
                                is_weekly_window_kind(kind)
                                and utilization == 0
                                and prev_last > 0
                            )
                        )
                    ):
                        await self.db.execute(
                            """INSERT INTO oauth_window_drop_log
                               (key_id, window_kind, resets_at, dropped_at,
                                prev_seen_at, from_utilization, to_utilization)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (key_id, kind, resets_at, now,
                             existing["last_seen_at"], prev_last, utilization),
                        )
                        reset_events.append({
                            "type": "utilization_drop",
                            "key_id": key_id,
                            "window_kind": kind,
                            "resets_at": resets_at,
                            "dropped_at": now,
                            "prev_seen_at": existing["last_seen_at"],
                            "from_utilization": prev_last,
                            "to_utilization": utilization,
                        })
                    max_util = existing["max_utilization"]
                    max_util_at = existing["max_utilization_at"]
                    if utilization is not None and (
                        max_util is None or utilization > max_util
                    ):
                        max_util, max_util_at = utilization, now
                    first_active = existing["first_active_at"]
                    if first_active is None and utilization is not None and utilization > 0:
                        first_active = now
                    last_util = (
                        utilization if utilization is not None
                        else existing["last_utilization"]
                    )
                    await self.db.execute(
                        """UPDATE oauth_window_log
                           SET last_seen_at = ?,
                               observations = observations + 1,
                               resets_at_raw = ?,
                               first_active_at = ?,
                               last_utilization = ?,
                               max_utilization = ?,
                               max_utilization_at = ?
                           WHERE id = ?""",
                        (now, obs["resets_at_raw"], first_active, last_util,
                         max_util, max_util_at, existing["id"]),
                    )
                    continue
                prev = latest
                active = utilization is not None and utilization > 0
                await self.db.execute(
                    """INSERT INTO oauth_window_log
                       (key_id, window_kind, resets_at, resets_at_raw, first_seen_at,
                        first_active_at, last_seen_at, observations,
                        last_utilization, max_utilization, max_utilization_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (key_id, kind, resets_at, obs["resets_at_raw"], now,
                     now if active else None, now, utilization, utilization,
                     now if utilization is not None else None),
                )
                await self._drain_window_pending(key_id, kind, resets_at)
                if prev:
                    reset_events.append({
                        "type": "window_reset",
                        "key_id": key_id,
                        "window_kind": kind,
                        "prev_resets_at": prev["resets_at"],
                        "new_resets_at": resets_at,
                        "span_days": _iso_span_days(prev["resets_at"], resets_at),
                    })
            await self.db.commit()
            return reset_events

    async def list_oauth_window_log(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT id, key_id, window_kind, resets_at, resets_at_raw,
                      first_seen_at, first_active_at, last_seen_at,
                      observations, last_utilization, max_utilization,
                      max_utilization_at
               FROM oauth_window_log
               WHERE key_id = ?
               ORDER BY window_kind ASC, resets_at ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def list_oauth_window_drops(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT id, key_id, window_kind, resets_at, dropped_at,
                      prev_seen_at, from_utilization, to_utilization
               FROM oauth_window_drop_log
               WHERE key_id = ?
               ORDER BY window_kind ASC, dropped_at ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def record_oauth_usage_snapshot(
        self,
        key_id: str,
        *,
        payload_hash: str,
        payload_json: str,
        headers_json: str,
        seen_at: str,
    ) -> int | None:
        """Store one usage payload, deduplicated by content.

        When the newest snapshot for this key has the same hash the payload has
        not changed: bump ``last_seen_at``/``seen_count`` instead of inserting.
        That distinguishes "this state was still being served at 17:59" from
        "we stopped polling at 17:40" — without it the row before a wipe proves
        nothing about when the pre-wipe state was last true.

        Returns the snapshot id the observation belongs to.
        """
        async with self.transaction():
            cur = await self.db.execute(
                """SELECT id, payload_hash FROM oauth_usage_snapshot
                   WHERE key_id = ?
                   ORDER BY first_seen_at DESC, id DESC LIMIT 1""",
                (key_id,),
            )
            latest = await cur.fetchone()
            if latest is not None and latest["payload_hash"] == payload_hash:
                await self.db.execute(
                    """UPDATE oauth_usage_snapshot
                       SET last_seen_at = ?, seen_count = seen_count + 1
                       WHERE id = ?""",
                    (seen_at, latest["id"]),
                )
                await self.db.commit()
                return int(latest["id"])
            await self.db.execute(
                """INSERT INTO oauth_usage_snapshot
                   (key_id, payload_hash, payload_json, headers_json,
                    first_seen_at, last_seen_at, seen_count)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (key_id, payload_hash, payload_json, headers_json,
                 seen_at, seen_at),
            )
            cur = await self.db.execute(
                """SELECT id FROM oauth_usage_snapshot
                   WHERE key_id = ? AND payload_hash = ? AND first_seen_at = ?
                   ORDER BY id DESC LIMIT 1""",
                (key_id, payload_hash, seen_at),
            )
            row = await cur.fetchone()
            await self.db.commit()
            return int(row["id"]) if row is not None else None

    async def record_oauth_limit_wipe(self, wipe: dict) -> None:
        """Record one observed limit wipe. See ``oauth_limit_wipe``."""
        async with self.transaction():
            await self.db.execute(
                """INSERT INTO oauth_limit_wipe
                   (key_id, window_kind, observed_at, prev_seen_at,
                    from_utilization, resets_at_claimed, hours_before_claimed,
                    five_hour_rolled, five_hour_early_minutes, source,
                    context_json, snapshot_id, prev_snapshot_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    wipe["key_id"],
                    wipe["window_kind"],
                    wipe["observed_at"],
                    wipe["prev_seen_at"],
                    wipe["from_utilization"],
                    wipe["resets_at_claimed"],
                    wipe.get("hours_before_claimed"),
                    1 if wipe.get("five_hour_rolled") else 0,
                    wipe.get("five_hour_early_minutes"),
                    wipe.get("source", "poll"),
                    wipe.get("context_json"),
                    wipe.get("snapshot_id"),
                    wipe.get("prev_snapshot_id"),
                ),
            )
            await self.db.commit()

    async def list_oauth_limit_wipes(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT id, key_id, window_kind, observed_at, prev_seen_at,
                      from_utilization, resets_at_claimed, hours_before_claimed,
                      five_hour_rolled, five_hour_early_minutes, source,
                      context_json, snapshot_id, prev_snapshot_id
               FROM oauth_limit_wipe
               WHERE key_id = ?
               ORDER BY observed_at ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def reconcile_limit_wipes_from_drops(self) -> list[dict]:
        """Derive wipe rows the detector did not write. Idempotent.

        Two writers feed ``oauth_limit_wipe``: the in-memory detector, and this.
        The detector is the one that alerts and links snapshots, but its state
        is empty for the first couple of minutes after a restart, while the
        drop-log writer compares against the database and has no such gap — so
        a wipe landing in that window reaches ``oauth_window_drop_log`` and
        nothing else. This closes that hole, and on its first run also recovers
        the events recorded before the wipe table existed.

        Rows written here carry ``source='backfill'``: they are derived, with no
        payload snapshot behind them, and must not be mistaken for detector
        output that has one. Returns the rows inserted, so the caller can alert
        on the recent ones.
        """
        cur = await self.db.execute(
            """SELECT key_id, window_kind, resets_at, dropped_at, prev_seen_at,
                      from_utilization
               FROM oauth_window_drop_log
               WHERE to_utilization = 0 AND from_utilization > 0
               ORDER BY dropped_at ASC"""
        )
        candidates = [
            dict(row) for row in await cur.fetchall()
            if is_weekly_window_kind(row["window_kind"])
        ]
        if not candidates:
            return []

        # seven_day and limit:weekly_all are the same upstream counter, so the
        # alias is dropped only where its primary is present for that instant —
        # never unconditionally, or a key reporting solely the alias would have
        # its wipes discarded.
        primary_at = {
            (c["key_id"], c["dropped_at"])
            for c in candidates
            if c["window_kind"] == "seven_day"
        }

        cur = await self.db.execute(
            "SELECT key_id, window_kind, observed_at FROM oauth_limit_wipe"
        )
        known = {
            (row["key_id"], row["window_kind"], row["observed_at"])
            for row in await cur.fetchall()
        }

        cur = await self.db.execute(
            """SELECT key_id, first_seen_at, resets_at_raw
               FROM oauth_window_log
               WHERE window_kind = 'five_hour'
               ORDER BY first_seen_at ASC"""
        )
        five_hour_rows = [dict(row) for row in await cur.fetchall()]

        inserted: list[dict] = []
        async with self.transaction():
            for candidate in candidates:
                kind = canonical_window_kind(candidate["window_kind"])
                if (
                    candidate["window_kind"] == "limit:weekly_all"
                    and (candidate["key_id"], candidate["dropped_at"]) in primary_at
                ):
                    continue
                identity = (candidate["key_id"], kind, candidate["dropped_at"])
                if identity in known:
                    continue

                dropped_at = candidate["dropped_at"]
                same_key = [
                    r for r in five_hour_rows if r["key_id"] == candidate["key_id"]
                ]
                # A 5h window born at the same instant as the drop. The bound is
                # one-sided on purpose: a window opening *after* the drop is a
                # separate event, and treating it as the roll would also make
                # the "previous" window the wrong one. Two independently cached
                # call sites can land observations for one key a minute apart,
                # so this is reachable.
                rolled_row = None
                for row in same_key:
                    delta = _iso_minutes_between(row["first_seen_at"], dropped_at)
                    if delta is not None and -0.1 <= delta <= 1.5:
                        rolled_row = row
                        break
                early_minutes = None
                if rolled_row is not None:
                    earlier = [
                        r for r in same_key
                        if r is not rolled_row
                        and r["first_seen_at"] < rolled_row["first_seen_at"]
                    ]
                    if earlier:
                        early_minutes = _iso_minutes_between(
                            dropped_at, earlier[-1]["resets_at_raw"])
                minutes_left = _iso_minutes_between(
                    dropped_at, candidate["resets_at"])

                row_values = {
                    "key_id": candidate["key_id"],
                    "window_kind": kind,
                    "observed_at": dropped_at,
                    "prev_seen_at": candidate["prev_seen_at"],
                    "from_utilization": candidate["from_utilization"],
                    "resets_at_claimed": candidate["resets_at"],
                    "hours_before_claimed": (
                        None if minutes_left is None else minutes_left / 60.0),
                    "five_hour_rolled": 1 if rolled_row is not None else 0,
                    "five_hour_early_minutes": early_minutes,
                    "source": "backfill",
                }
                await self.db.execute(
                    """INSERT INTO oauth_limit_wipe
                       (key_id, window_kind, observed_at, prev_seen_at,
                        from_utilization, resets_at_claimed, hours_before_claimed,
                        five_hour_rolled, five_hour_early_minutes, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'backfill')""",
                    (row_values["key_id"], row_values["window_kind"],
                     row_values["observed_at"], row_values["prev_seen_at"],
                     row_values["from_utilization"],
                     row_values["resets_at_claimed"],
                     row_values["hours_before_claimed"],
                     row_values["five_hour_rolled"],
                     row_values["five_hour_early_minutes"]),
                )
                known.add(identity)
                inserted.append(row_values)

            if inserted:
                await self.db.commit()
        return inserted

    async def prune_oauth_usage_snapshots(self, *, before: str) -> int:
        """Drop snapshots older than *before*, keeping any a wipe points at.

        Returns the number of rows deleted.
        """
        async with self.transaction():
            cur = await self.db.execute(
                """DELETE FROM oauth_usage_snapshot
                   WHERE last_seen_at < ?
                     AND id NOT IN (
                         SELECT snapshot_id FROM oauth_limit_wipe
                         WHERE snapshot_id IS NOT NULL
                         UNION
                         SELECT prev_snapshot_id FROM oauth_limit_wipe
                         WHERE prev_snapshot_id IS NOT NULL
                     )""",
                (before,),
            )
            deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            await self.db.commit()
            return int(deleted)

    async def attribute_oauth_window_usage(
        self,
        deltas: list[dict],
        *,
        now: str | None = None,
    ) -> None:
        """Attribute per-(key, model) usage deltas to observed rate-limit windows.

        For the latest observed window of every kind of ``delta["key_id"]``:
        still live -> increment ``oauth_window_usage``; expired -> increment
        ``oauth_window_usage_pending`` (drained into the next observed window
        by ``record_oauth_window_observations``); kind never observed -> the
        delta is skipped and remains visible in ``usage_daily`` only.
        """
        async with self.transaction():
            if not deltas:
                return
            now_iso = now or datetime.now(timezone.utc).isoformat()
            now_dt = _parse_iso_utc(now_iso)
            usage_sql = build_window_usage_upsert_sql(self._backend)
            pending_sql = build_window_pending_upsert_sql(self._backend)
            latest_by_key: dict[str, list[dict]] = {}
            changed = False
            for delta in deltas:
                key_id = delta["key_id"]
                counters = tuple(int(delta.get(c, 0)) for c in WINDOW_USAGE_COUNTERS)
                if not any(counters):
                    continue
                if key_id not in latest_by_key:
                    cur = await self.db.execute(
                        """SELECT w.id, w.window_kind, w.resets_at, w.resets_at_raw
                           FROM oauth_window_log w
                           JOIN (SELECT window_kind, MAX(resets_at) AS max_resets_at
                                 FROM oauth_window_log
                                 WHERE key_id = ?
                                 GROUP BY window_kind) latest
                             ON latest.window_kind = w.window_kind
                            AND latest.max_resets_at = w.resets_at
                           WHERE w.key_id = ?""",
                        (key_id, key_id),
                    )
                    latest_by_key[key_id] = [dict(r) for r in await cur.fetchall()]
                for win in latest_by_key[key_id]:
                    if win["window_kind"].startswith("limit:"):
                        continue
                    end = (
                        _parse_iso_utc(win["resets_at_raw"])
                        or _parse_iso_utc(win["resets_at"])
                    )
                    if end is not None and now_dt is not None and now_dt <= end:
                        await self.db.execute(
                            usage_sql, (win["id"], delta["model"], *counters)
                        )
                    else:
                        await self.db.execute(
                            pending_sql,
                            (key_id, win["window_kind"], delta["model"],
                             *counters, now_iso),
                        )
                    changed = True
            if changed:
                await self.db.commit()

    async def _drain_window_pending(
        self, key_id: str, window_kind: str, resets_at: str
    ) -> None:
        """Move pending usage for (key, kind) into the just-inserted window row."""
        cur = await self.db.execute(
            "SELECT id FROM oauth_window_log "
            "WHERE key_id = ? AND window_kind = ? AND resets_at = ?",
            (key_id, window_kind, resets_at),
        )
        row = await cur.fetchone()
        if row is None:
            return
        window_id = row["id"]
        cur = await self.db.execute(
            """DELETE FROM oauth_window_usage_pending
               WHERE key_id = ? AND window_kind = ?
               RETURNING model, input_tokens, output_tokens, cache_read_tokens,
                         cache_creation_tokens, cache_creation_5m_tokens,
                         cache_creation_1h_tokens, web_search_requests, requests""",
            (key_id, window_kind),
        )
        pending = await cur.fetchall()
        if not pending:
            return
        usage_sql = build_window_usage_upsert_sql(self._backend)
        for p in pending:
            await self.db.execute(
                usage_sql,
                (window_id, p["model"],
                 *(p[c] for c in WINDOW_USAGE_COUNTERS)),
            )

    async def list_oauth_window_usage(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT u.window_id, u.model, u.input_tokens, u.output_tokens,
                      u.cache_read_tokens, u.cache_creation_tokens,
                      u.cache_creation_5m_tokens, u.cache_creation_1h_tokens,
                      u.web_search_requests, u.requests
               FROM oauth_window_usage u
               JOIN oauth_window_log w ON w.id = u.window_id
               WHERE w.key_id = ?
               ORDER BY u.window_id ASC, u.model ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def list_oauth_window_usage_pending(self, key_id: str) -> list[dict]:
        cur = await self.db.execute(
            """SELECT key_id, window_kind, model, input_tokens, output_tokens,
                      cache_read_tokens, cache_creation_tokens,
                      cache_creation_5m_tokens, cache_creation_1h_tokens,
                      web_search_requests, requests, updated_at
               FROM oauth_window_usage_pending
               WHERE key_id = ?
               ORDER BY window_kind ASC, model ASC""",
            (key_id,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def export_snapshot(self) -> dict[str, list[dict]]:
        snapshot: dict[str, list[dict]] = {}
        for table, columns, order_by in SNAPSHOT_TABLE_SPECS:
            snapshot[table] = await self._export_table_rows(table, columns, order_by)
        return snapshot

    async def replace_snapshot(self, snapshot: dict[str, list[dict]]) -> None:
        # A savepoint needs an enclosing transaction, which autocommit removes
        # on the Postgres backend; an explicit transaction is equivalent here.
        async with self.transaction():
            for table in SNAPSHOT_DELETE_ORDER:
                await self.db.execute(f"DELETE FROM {table}")
            for table, columns, _order_by in SNAPSHOT_TABLE_SPECS:
                await self._replace_table_rows(table, columns, snapshot.get(table, []))
            await self._after_replace_snapshot(snapshot)


def build_database_from_config(*, database_url: str = "", db_path: str = "./smart-proxy.db") -> Database:
    if database_url:
        from smart_proxy.db_postgres import PostgresDatabase

        return PostgresDatabase(database_url)
    return Database(db_path)


def build_database(settings) -> Database:  # noqa: ANN001
    return build_database_from_config(
        database_url=getattr(settings, "database_url", ""),
        db_path=settings.db_path,
    )
