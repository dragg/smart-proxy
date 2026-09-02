from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys

from datetime import datetime, timedelta, timezone


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ------------------------------------------------------------------
# proxy-key CLI
# ------------------------------------------------------------------

async def _proxy_key_cmd(args: list[str]) -> None:
    from smart_proxy.config import get_settings
    from smart_proxy.db import build_database

    if not args:
        _proxy_key_usage()
        return

    settings = get_settings()
    db = build_database(settings)
    await db.connect()

    try:
        action = args[0]
        if action == "add":
            name = args[1] if len(args) > 1 else "default"
            key = f"sp-{secrets.token_hex(16)}"
            await db.add_proxy_key(key, name)
            print(key)
        elif action == "list":
            rows = await db.list_proxy_keys()
            if not rows:
                print("No proxy API keys.")
                return
            print(f"{'KEY':<16} {'NAME':<20} {'ACTIVE':<8} {'CREATED'}")
            for r in rows:
                masked = r["key"][:12] + "…"
                active = "yes" if r["active"] else "REVOKED"
                print(f"{masked:<16} {r['name']:<20} {active:<8} {r['created_at'][:19]}")
        elif action == "revoke":
            if len(args) < 2 or len(args[1]) < 6:
                print("Usage: proxy-key revoke <prefix>  (at least 6 chars)", file=sys.stderr)
                sys.exit(1)
            full = await db.revoke_proxy_key(args[1])
            if full:
                print(f"Revoked: {full[:12]}…")
            else:
                print("No matching active key found (or ambiguous prefix).", file=sys.stderr)
                sys.exit(1)
        else:
            _proxy_key_usage()
    finally:
        await db.close()


# ------------------------------------------------------------------
# usage CLI
# ------------------------------------------------------------------

async def _usage_cmd(args: list[str]) -> None:
    from smart_proxy.config import get_settings
    from smart_proxy.db import build_database
    from smart_proxy.usage import build_price_lookup

    days = 7
    by_key = False
    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
            i += 2
        elif args[i] == "--by-key":
            by_key = True
            i += 1
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            print("Usage: python -m smart_proxy usage [--days N] [--by-key]", file=sys.stderr)
            sys.exit(1)

    settings = get_settings()
    db = build_database(settings)
    await db.connect()

    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")

        rows = await db.query_usage(start_str, end_str, by_key=by_key)
        if not rows:
            print(f"No usage data for the last {days} days.")
            return
        prices = build_price_lookup(await db.get_all_model_prices())

        if by_key:
            _print_usage_by_key(rows, prices)
        else:
            _print_usage_by_model(rows, prices)
    finally:
        await db.close()


def _print_usage_by_model(rows: list[dict], prices: dict | None = None) -> None:
    from smart_proxy.usage import estimate_cost_with_cache

    print(
        f"{'DATE':<12} {'MODEL':<24} {'REQ':>5} "
        f"{'INPUT_TOK':>12} {'OUTPUT_TOK':>12} "
        f"{'CACHE_R':>10} {'CACHE_W':>10} {'W_5M':>10} {'W_1H':>10} {'WEB_SRCH':>9} {'COST':>10}"
    )
    print("-" * 142)

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_cache_creation_5m = 0
    total_cache_creation_1h = 0
    total_web_search = 0
    total_reqs = 0
    total_cost = 0.0
    has_unknown = False
    has_partial = False

    for r in rows:
        inp = r["input_tokens"]
        out = r["output_tokens"]
        cache_read = r.get("cache_read_tokens", 0)
        cache_creation = r.get("cache_creation_tokens", 0)
        cache_creation_5m = r.get("cache_creation_5m_tokens", 0)
        cache_creation_1h = r.get("cache_creation_1h_tokens", 0)
        web_search = r.get("web_search_requests", 0)
        reqs = r["requests"]
        cost, partial = estimate_cost_with_cache(
            r["model"],
            inp,
            out,
            cache_read,
            cache_creation,
            cache_creation_5m,
            cache_creation_1h,
            web_search,
            prices=prices,
        )
        total_input += inp
        total_output += out
        total_cache_read += cache_read
        total_cache_creation += cache_creation
        total_cache_creation_5m += cache_creation_5m
        total_cache_creation_1h += cache_creation_1h
        total_web_search += web_search
        total_reqs += reqs

        if cost is not None:
            total_cost += cost
            cost_str = f"{'~' if partial else ''}${cost:,.2f}"
            has_partial = has_partial or partial
        else:
            has_unknown = True
            cost_str = "?"

        print(
            f"{r['date']:<12} {r['model']:<24} {reqs:>5,} "
            f"{inp:>12,} {out:>12,} "
            f"{cache_read:>10,} {cache_creation:>10,} "
            f"{cache_creation_5m:>10,} {cache_creation_1h:>10,} {web_search:>9,} {cost_str:>10}"
        )

    print("-" * 142)
    total_cost_str = f"${total_cost:,.2f}"
    if has_partial:
        total_cost_str = "~" + total_cost_str
    if has_unknown:
        total_cost_str += " *"
    print(
        f"{'TOTAL':<12} {'':<24} {total_reqs:>5,} "
        f"{total_input:>12,} {total_output:>12,} "
        f"{total_cache_read:>10,} {total_cache_creation:>10,} "
        f"{total_cache_creation_5m:>10,} {total_cache_creation_1h:>10,} {total_web_search:>9,} {total_cost_str:>10}"
    )
    if has_unknown:
        print("\n* Some models have unknown pricing; total cost is partial.")
    if has_partial:
        print("~ Anthropic cache write cost is estimated for legacy rows without 5m/1h split.")


def _print_usage_by_key(rows: list[dict], prices: dict | None = None) -> None:
    from smart_proxy.usage import estimate_cost_with_cache

    print(
        f"{'DATE':<12} {'GROUP':<18} {'KEY':<16} {'MODEL':<24} {'REQ':>5} "
        f"{'INPUT_TOK':>12} {'OUTPUT_TOK':>12} "
        f"{'CACHE_R':>10} {'CACHE_W':>10} {'W_5M':>10} {'W_1H':>10} {'WEB_SRCH':>9} {'COST':>10}"
    )
    print("-" * 177)

    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_cache_creation_5m = 0
    total_cache_creation_1h = 0
    total_web_search = 0
    total_reqs = 0
    total_cost = 0.0
    has_unknown = False
    has_partial = False

    for r in rows:
        inp = r["input_tokens"]
        out = r["output_tokens"]
        cache_read = r.get("cache_read_tokens", 0)
        cache_creation = r.get("cache_creation_tokens", 0)
        cache_creation_5m = r.get("cache_creation_5m_tokens", 0)
        cache_creation_1h = r.get("cache_creation_1h_tokens", 0)
        web_search = r.get("web_search_requests", 0)
        reqs = r["requests"]
        cost, partial = estimate_cost_with_cache(
            r["model"],
            inp,
            out,
            cache_read,
            cache_creation,
            cache_creation_5m,
            cache_creation_1h,
            web_search,
            prices=prices,
        )
        total_input += inp
        total_output += out
        total_cache_read += cache_read
        total_cache_creation += cache_creation
        total_cache_creation_5m += cache_creation_5m
        total_cache_creation_1h += cache_creation_1h
        total_web_search += web_search
        total_reqs += reqs

        key_label = r["key_name"] or r["proxy_key"][:12] or "(no auth)"
        group_label = (r.get("group_name") or "")[:18]
        if cost is not None:
            total_cost += cost
            cost_str = f"{'~' if partial else ''}${cost:,.2f}"
            has_partial = has_partial or partial
        else:
            has_unknown = True
            cost_str = "?"

        print(
            f"{r['date']:<12} {group_label:<18} {key_label:<16} {r['model']:<24} {reqs:>5,} "
            f"{inp:>12,} {out:>12,} "
            f"{cache_read:>10,} {cache_creation:>10,} "
            f"{cache_creation_5m:>10,} {cache_creation_1h:>10,} {web_search:>9,} {cost_str:>10}"
        )

    print("-" * 177)
    total_cost_str = f"${total_cost:,.2f}"
    if has_partial:
        total_cost_str = "~" + total_cost_str
    if has_unknown:
        total_cost_str += " *"
    print(
        f"{'TOTAL':<12} {'':<18} {'':<16} {'':<24} {total_reqs:>5,} "
        f"{total_input:>12,} {total_output:>12,} "
        f"{total_cache_read:>10,} {total_cache_creation:>10,} "
        f"{total_cache_creation_5m:>10,} {total_cache_creation_1h:>10,} {total_web_search:>9,} {total_cost_str:>10}"
    )
    if has_unknown:
        print("\n* Some models have unknown pricing; total cost is partial.")
    if has_partial:
        print("~ Anthropic cache write cost is estimated for legacy rows without 5m/1h split.")


def _proxy_key_usage() -> None:
    print(
        "Usage: python -m smart_proxy proxy-key <command>\n"
        "\n"
        "Commands:\n"
        "  add [NAME]       Generate a new proxy API key\n"
        "  list             Show all proxy API keys\n"
        "  revoke PREFIX    Deactivate a key by prefix (>= 6 chars)\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ------------------------------------------------------------------
# price CLI
# ------------------------------------------------------------------

async def _price_cmd(args: list[str]) -> None:
    from smart_proxy.config import get_settings
    from smart_proxy.db import build_database
    from smart_proxy.usage import build_default_price_rows, build_price_lookup, calculate_cost

    if not args:
        _price_usage()
        return

    settings = get_settings()
    db = build_database(settings)
    await db.connect()
    try:
        action = args[0]
        if action == "list":
            rows = await db.get_all_model_prices()
            if not rows:
                print("No model prices configured.")
                return
            print(
                f"{'PREFIX':<24} {'PROVIDER':<10} {'INPUT':>8} {'OUTPUT':>8} "
                f"{'CACHE_R':>8} {'W_5M':>8} {'W_1H':>8}"
            )
            print("-" * 84)
            for r in rows:
                def _fmt(v: float | None) -> str:
                    return "-" if v is None else f"{v:.2f}"

                print(
                    f"{r['model_prefix']:<24} {r['provider']:<10} "
                    f"{r['input_price']:>8.2f} {r['output_price']:>8.2f} "
                    f"{_fmt(r['cache_read_price']):>8} "
                    f"{_fmt(r['cache_write_5m_price']):>8} "
                    f"{_fmt(r['cache_write_1h_price']):>8}"
                )
            return

        if action == "seed":
            inserted = await db.seed_model_prices(build_default_price_rows())
            print(f"Seed completed (inserted default price rows: {inserted}).")
            return

        if action == "set":
            if len(args) < 2:
                _price_usage()
            model_prefix = args[1]
            provider = ""
            input_price: float | None = None
            output_price: float | None = None
            cache_read: float | None = None
            cache_w5: float | None = None
            cache_w1: float | None = None
            i = 2
            while i < len(args):
                if args[i] == "--provider" and i + 1 < len(args):
                    provider = args[i + 1]
                    i += 2
                elif args[i] == "--input" and i + 1 < len(args):
                    input_price = float(args[i + 1])
                    i += 2
                elif args[i] == "--output" and i + 1 < len(args):
                    output_price = float(args[i + 1])
                    i += 2
                elif args[i] == "--cache-read" and i + 1 < len(args):
                    cache_read = float(args[i + 1])
                    i += 2
                elif args[i] == "--cache-write-5m" and i + 1 < len(args):
                    cache_w5 = float(args[i + 1])
                    i += 2
                elif args[i] == "--cache-write-1h" and i + 1 < len(args):
                    cache_w1 = float(args[i + 1])
                    i += 2
                else:
                    print(f"Unknown argument: {args[i]}", file=sys.stderr)
                    _price_usage()

            if not provider or input_price is None or output_price is None:
                print("Missing required args: --provider --input --output", file=sys.stderr)
                _price_usage()

            await db.upsert_model_price(
                model_prefix=model_prefix,
                provider=provider,
                input_price=input_price,
                output_price=output_price,
                cache_read_price=cache_read,
                cache_write_5m_price=cache_w5,
                cache_write_1h_price=cache_w1,
            )
            print(f"Price upserted: {model_prefix}")
            return

        if action == "calc":
            if len(args) < 2:
                _price_usage()
            model = args[1]
            input_tokens = 0
            output_tokens = 0
            cache_read = 0
            cache_w5 = 0
            cache_w1 = 0
            web_search = 0
            i = 2
            while i < len(args):
                if args[i] == "--input" and i + 1 < len(args):
                    input_tokens = int(args[i + 1])
                    i += 2
                elif args[i] == "--output" and i + 1 < len(args):
                    output_tokens = int(args[i + 1])
                    i += 2
                elif args[i] == "--cache-read" and i + 1 < len(args):
                    cache_read = int(args[i + 1])
                    i += 2
                elif args[i] == "--cache-write-5m" and i + 1 < len(args):
                    cache_w5 = int(args[i + 1])
                    i += 2
                elif args[i] == "--cache-write-1h" and i + 1 < len(args):
                    cache_w1 = int(args[i + 1])
                    i += 2
                elif args[i] == "--web-search" and i + 1 < len(args):
                    web_search = int(args[i + 1])
                    i += 2
                else:
                    print(f"Unknown argument: {args[i]}", file=sys.stderr)
                    _price_usage()

            prices = build_price_lookup(await db.get_all_model_prices())
            breakdown = calculate_cost(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=(cache_w5 + cache_w1),
                cache_creation_5m_tokens=cache_w5,
                cache_creation_1h_tokens=cache_w1,
                web_search_requests=web_search,
                prices=prices,
            )
            if breakdown.unknown_pricing or breakdown.total_cost is None:
                print(f"Unknown pricing for model: {model}", file=sys.stderr)
                sys.exit(2)

            print(f"Model:           {model}")
            print(f"Matched prefix:  {breakdown.matched_prefix}")
            print(f"Provider:        {breakdown.provider}")
            print(f"Input:           {input_tokens:>12,} tok  =  ${breakdown.input_cost:,.4f}")
            print(f"Output:          {output_tokens:>12,} tok  =  ${breakdown.output_cost:,.4f}")
            print(f"Cache read:      {cache_read:>12,} tok  =  ${breakdown.cache_read_cost:,.4f}")
            print(f"Cache write 5m:  {cache_w5:>12,} tok  =  ${breakdown.cache_write_5m_cost:,.4f}")
            print(f"Cache write 1h:  {cache_w1:>12,} tok  =  ${breakdown.cache_write_1h_cost:,.4f}")
            print(f"Web search:      {web_search:>12,} req  =  ${breakdown.web_search_cost:,.4f}")
            print("-" * 52)
            suffix = " (partial)" if breakdown.partial else ""
            print(f"TOTAL: ${breakdown.total_cost:,.4f}{suffix}")
            return

        _price_usage()
    finally:
        await db.close()


def _price_usage() -> None:
    print(
        "Usage: python -m smart_proxy price <command>\n"
        "\n"
        "Commands:\n"
        "  list\n"
        "  seed\n"
        "  set <model_prefix> --provider <name> --input <usd_per_1m> --output <usd_per_1m>\n"
        "      [--cache-read <usd_per_1m>] [--cache-write-5m <usd_per_1m>] [--cache-write-1h <usd_per_1m>]\n"
        "  calc <model> [--input N] [--output N] [--cache-read N] [--cache-write-5m N] [--cache-write-1h N] [--web-search N]\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ------------------------------------------------------------------
# db CLI
# ------------------------------------------------------------------

def _db_usage() -> None:
    print(
        "Usage: python -m smart_proxy db <command>\n"
        "\n"
        "Commands:\n"
        "  migrate                         Apply PostgreSQL schema migrations\n"
        "  backfill-wipes                  Derive missing oauth_limit_wipe rows\n"
        "                                  from the drop log (idempotent; also\n"
        "                                  runs hourly inside the proxy)\n",
        file=sys.stderr,
    )
    sys.exit(1)


def _redact_database_url(database_url: str) -> str:
    """Host and database only — never the credentials."""
    import re

    return re.sub(r"//[^@/]*@", "//", database_url)


def _require_database_url(database_url: str) -> str:
    if not database_url:
        print(
            "DATABASE_URL is required for PostgreSQL migration/import commands.",
            file=sys.stderr,
        )
        sys.exit(1)
    return database_url


async def _db_cmd(args: list[str]) -> None:
    from smart_proxy.config import get_settings
    from smart_proxy.db_migrations import run_postgres_migrations

    if not args:
        _db_usage()
        return

    settings = get_settings()
    action = args[0]

    if action == "migrate":
        if len(args) != 1:
            _db_usage()
        database_url = _require_database_url(settings.database_url)
        applied = await run_postgres_migrations(database_url)
        if applied:
            print("Applied PostgreSQL migrations:")
            for name in applied:
                print(f"  - {name}")
        else:
            print("PostgreSQL schema is already up to date.")
        return

    if action == "backfill-wipes":
        if len(args) != 1:
            _db_usage()
        from pathlib import Path

        from smart_proxy.db import build_database

        # Always say which database was opened. Settings read .env relative to
        # the working directory, so running this from anywhere but the repo
        # root on a PostgreSQL host would otherwise fall through to SQLite,
        # create an empty ./smart-proxy.db, and report an all-clear about production
        # data it never touched.
        if settings.database_url:
            target = _redact_database_url(settings.database_url)
            print(f"Database: PostgreSQL {target}")
        else:
            db_path = Path(settings.db_path).resolve()
            print(f"Database: SQLite {db_path}")
            if not db_path.exists():
                print(
                    f"{db_path} does not exist. Refusing to create an empty "
                    "database and report an all-clear — set DATABASE_URL or "
                    "run from the directory holding your .env / smart-proxy.db.",
                    file=sys.stderr,
                )
                sys.exit(1)

        db = build_database(settings)
        await db.connect()
        try:
            inserted = await db.reconcile_limit_wipes_from_drops()
        finally:
            await db.close()
        if inserted:
            print(f"Recovered {len(inserted)} limit wipe(s) from the drop log:")
            for wipe in inserted:
                print(f"  - {wipe['observed_at']}  {wipe['window_kind']}  "
                      f"{wipe['from_utilization']}% -> 0%")
        else:
            print("No missing limit wipes: oauth_limit_wipe is already complete.")
        return

    _db_usage()


# ------------------------------------------------------------------
# anthropic-key CLI
# ------------------------------------------------------------------

async def _anthropic_key_cmd(args: list[str]) -> None:
    import json
    import uuid
    from pathlib import Path

    from smart_proxy.config import get_settings
    from smart_proxy.db import build_database

    if not args:
        _anthropic_key_usage()
        return

    settings = get_settings()
    db = build_database(settings)
    await db.connect()

    try:
        action = args[0]

        if action == "add-oauth":
            if len(args) < 2:
                print("Usage: anthropic-key add-oauth <oauth.json> [--name LABEL]", file=sys.stderr)
                sys.exit(1)
            json_path = Path(args[1])
            if not json_path.exists():
                print(f"File not found: {json_path}", file=sys.stderr)
                sys.exit(1)

            name = ""
            if "--name" in args:
                idx = args.index("--name")
                if idx + 1 < len(args):
                    name = args[idx + 1]

            data = json.loads(json_path.read_text())
            oauth = data.get("claudeAiOauth", data)

            key_id = str(uuid.uuid4())
            await db.insert_anthropic_key(
                id=key_id,
                key_type="oauth",
                access_token=oauth.get("accessToken") or oauth.get("access_token", ""),
                refresh_token=oauth.get("refreshToken") or oauth.get("refresh_token", ""),
                expires_at=oauth.get("expiresAt") or oauth.get("expires_at"),
                scopes=json.dumps(oauth.get("scopes", [])),
                subscription_type=oauth.get("subscriptionType") or oauth.get("subscription_type", ""),
                rate_limit_tier=oauth.get("rateLimitTier") or oauth.get("rate_limit_tier", ""),
                name=name or json_path.stem,
            )
            print(f"Added OAuth key: {key_id}")

        elif action == "add-apikey":
            if len(args) < 2:
                print("Usage: anthropic-key add-apikey <sk-ant-...> [--name LABEL]", file=sys.stderr)
                sys.exit(1)
            api_key = args[1]

            name = ""
            if "--name" in args:
                idx = args.index("--name")
                if idx + 1 < len(args):
                    name = args[idx + 1]

            key_id = str(uuid.uuid4())
            await db.insert_anthropic_key(
                id=key_id,
                key_type="api_key",
                api_key=api_key,
                name=name or f"apikey-{api_key[:12]}",
            )
            print(f"Added API key: {key_id}")

        elif action == "list":
            rows = await db.list_anthropic_keys()
            if not rows:
                print("No Anthropic keys.")
                return
            print(f"{'ID':<14} {'TYPE':<8} {'STATUS':<10} {'NAME':<20} {'EXPIRES'}")
            print("-" * 75)
            for r in rows:
                kid = r["id"][:12] + ".."
                exp = ""
                if r["expires_at"]:
                    exp_dt = datetime.fromtimestamp(
                        r["expires_at"] / 1000, tz=timezone.utc
                    )
                    exp = exp_dt.strftime("%Y-%m-%d %H:%M UTC")
                print(f"{kid:<14} {r['key_type']:<8} {r['status']:<10} {r['name']:<20} {exp}")

        elif action == "deactivate":
            if len(args) < 2 or len(args[1]) < 6:
                print("Usage: anthropic-key deactivate <id-prefix>  (>= 6 chars)", file=sys.stderr)
                sys.exit(1)
            full = await db.deactivate_anthropic_key_by_prefix(
                args[1],
                audit_op_id=str(uuid.uuid4()),
                audit_source="manual_cli",
                audit_event_type="status_change",
                audit_decision="deactivate",
                audit_error_type="manual_action",
                audit_error_message="Deactivated via anthropic-key CLI",
                audit_context={"command": "anthropic-key deactivate"},
            )
            if full:
                print(f"Deactivated: {full[:12]}..")
            else:
                print("No matching active key (or ambiguous prefix).", file=sys.stderr)
                sys.exit(1)

        elif action == "activate":
            if len(args) < 2 or len(args[1]) < 6:
                print("Usage: anthropic-key activate <id-prefix>  (>= 6 chars)", file=sys.stderr)
                sys.exit(1)
            prefix = args[1]
            full_id = await db.activate_anthropic_key_by_prefix(
                prefix,
                audit_op_id=str(uuid.uuid4()),
                audit_source="manual_cli",
                audit_event_type="status_change",
                audit_decision="activate",
                audit_error_type="manual_action",
                audit_error_message="Activated via anthropic-key CLI",
                audit_context={"command": "anthropic-key activate"},
            )
            if not full_id:
                print("No matching key (or ambiguous prefix).", file=sys.stderr)
                sys.exit(1)
            print(f"Activated: {full_id[:12]}..")

        else:
            _anthropic_key_usage()
    finally:
        await db.close()

    if action in ("add-oauth", "add-apikey", "deactivate", "activate"):
        await _notify_proxy_reload()


async def _notify_proxy_reload() -> None:
    """Tell the running Anthropic proxy to reload keys from DB."""
    import httpx as _httpx
    from urllib.parse import quote

    from smart_proxy.config import get_settings

    port = os.environ.get("ANTHROPIC_PROXY_PORT", "8090")
    reload_key = get_settings().anthropic_proxy_reload_key.strip()
    qs = f"?key={quote(reload_key)}" if reload_key else ""
    try:
        async with _httpx.AsyncClient() as c:
            r = await c.post(f"http://127.0.0.1:{port}/_reload{qs}", timeout=5)
            data = r.json()
            print(f"Proxy reloaded ({data.get('active', '?')} active keys)")
    except Exception:
        print("(proxy not running or unreachable — reload skipped)")


def _anthropic_key_usage() -> None:
    print(
        "Usage: python -m smart_proxy anthropic-key <command>\n"
        "\n"
        "Commands:\n"
        "  add-oauth <oauth.json> [--name LABEL]   Import OAuth key from JSON\n"
        "  add-apikey <sk-ant-...> [--name LABEL]   Add a plain API key\n"
        "  list                                     Show all Anthropic keys\n"
        "  deactivate <id-prefix>                   Mark a key inactive\n"
        "  activate <id-prefix>                     Restore a key to active\n",
        file=sys.stderr,
    )
    sys.exit(1)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def _main_usage() -> None:
    print(
        "Usage: python -m smart_proxy <command> [args]\n"
        "\n"
        "Commands:\n"
        "  anthropic-proxy        Run the Anthropic reverse proxy (+ OpenAI-compatible endpoint)\n"
        "  anthropic-key          Manage Anthropic keys (OAuth / API key)\n"
        "  anthropic-login        Interactive Anthropic OAuth login\n"
        "  oauth-refresh          Refresh Anthropic OAuth tokens\n"
        "  inspect-oauth-token    Inspect a stored OAuth token\n"
        "  proxy-key              Manage sp-* proxy API keys\n"
        "  usage                  Show the usage / cost report\n"
        "  price                  Manage model prices\n"
        "  db                     Apply PostgreSQL schema migrations\n"
        "  anthropic-debug-proxy  Run the debug / capture proxy\n"
        "  analyze-capture-dir    Analyze a capture directory\n",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] == "proxy-key":
        asyncio.run(_proxy_key_cmd(args[1:]))
        return

    if args and args[0] == "usage":
        asyncio.run(_usage_cmd(args[1:]))
        return

    if args and args[0] == "price":
        asyncio.run(_price_cmd(args[1:]))
        return

    if args and args[0] == "anthropic-key":
        asyncio.run(_anthropic_key_cmd(args[1:]))
        return

    if args and args[0] == "db":
        asyncio.run(_db_cmd(args[1:]))
        return

    if args and args[0] == "anthropic-login":
        from smart_proxy.anthropic_login import main as anthropic_login_main

        anthropic_login_main(args[1:])
        return

    if args and args[0] == "anthropic-proxy":
        from smart_proxy.anthropic_proxy import main as anthropic_proxy_main

        anthropic_proxy_main()
        return

    if args and args[0] == "anthropic-debug-proxy":
        from smart_proxy.anthropic_debug_proxy import main_sync

        main_sync()
        return

    if args and args[0] == "inspect-oauth-token":
        from smart_proxy.oauth_token_inspect import main as oauth_inspect_main

        oauth_inspect_main(args[1:])
        return

    if args and args[0] == "analyze-capture-dir":
        from smart_proxy.analyze_capture_dir import main as analyze_capture_main

        analyze_capture_main(args[1:])
        return

    if args and args[0] == "oauth-refresh":
        from smart_proxy.oauth_refresh_cli import main as oauth_refresh_main

        oauth_refresh_main(args[1:])
        return

    _main_usage()


if __name__ == "__main__":
    main()
