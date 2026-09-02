#!/usr/bin/env python3
"""Probe Claude OAuth-related endpoints with current token.

Defaults:
- Token source: latest OAuth token from `DATABASE_URL` or ../smart-proxy.db
- Target base URL: https://api.anthropic.com

This script is read-only: it does not modify DB or keychain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.anthropic_oauth import build_activation_requests
from smart_proxy.db import build_database_from_config


async def _pick_oauth_token(
    db_path: Path,
    database_url: str,
    id_prefix: str | None,
) -> tuple[str, str, str]:
    db = build_database_from_config(
        database_url=database_url,
        db_path=str(db_path),
    )
    await db.connect()
    try:
        rows = await db.list_anthropic_keys()
        rows = [
            row
            for row in rows
            if row.get("key_type") == "oauth" and (row.get("access_token") or "").strip()
        ]
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        if id_prefix:
            prefix = id_prefix.lower()
            rows = [
                row
                for row in rows
                if str(row.get("id", "")).lower().startswith(prefix)
            ]
        if not rows:
            raise RuntimeError("No OAuth key with access_token found in DB.")
        if len(rows) > 1 and id_prefix:
            ids = ", ".join(str(r["id"])[:12] for r in rows[:5])
            raise RuntimeError(f"Ambiguous --id-prefix, multiple keys match: {ids}")
        row = rows[0]
        return str(row["id"]), str(row["access_token"]), str(row["status"])
    finally:
        await db.close()


def _summarize_json(obj: object) -> str:
    if isinstance(obj, dict):
        keys = list(obj.keys())[:8]
        return f"json keys={keys}"
    if isinstance(obj, list):
        return f"json list len={len(obj)}"
    return str(type(obj).__name__)


def _extract_error_details(resp: httpx.Response) -> str:
    req_id = (
        resp.headers.get("request-id")
        or resp.headers.get("x-request-id")
        or resp.headers.get("anthropic-request-id")
        or ""
    )
    try:
        data = resp.json()
    except Exception:
        txt = resp.text[:300].replace("\n", " ")
        return f"request_id={req_id or '-'} body={txt!r}"

    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            et = err.get("type", "")
            msg = str(err.get("message", "")).replace("\n", " ")
            rid = data.get("request_id") or req_id or "-"
            return f"error.type={et or '-'} request_id={rid} message={msg or '-'}"
        rid = data.get("request_id") or req_id or "-"
        return f"request_id={rid} {_summarize_json(data)}"
    return f"request_id={req_id or '-'} {_summarize_json(data)}"


def main() -> None:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Probe Anthropic OAuth endpoints")
    parser.add_argument("--db", default=str((here.parent / "smart-proxy.db")), help="Path to smart-proxy.db")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Optional PostgreSQL DATABASE_URL override",
    )
    parser.add_argument("--id-prefix", default="", help="Optional OAuth key id prefix")
    parser.add_argument("--base-url", default="https://api.anthropic.com", help="API base URL")
    parser.add_argument("--token", default="", help="Optional access token override")
    parser.add_argument("--timeout", type=float, default=20.0, help="Request timeout seconds")
    args = parser.parse_args()

    token = args.token.strip()
    key_id = "(manual)"
    status = "(n/a)"
    if not token:
        key_id, token, status = asyncio.run(
            _pick_oauth_token(
                Path(args.db),
                args.database_url.strip(),
                args.id_prefix.strip() or None,
            )
        )

    print(f"Using key: {key_id[:12]}.. status={status}")
    print(f"Base URL:  {args.base_url}")
    print("-" * 110)
    print(f"{'METHOD':<6} {'PATH':<56} {'HTTP':<5} RESULT")
    print("-" * 110)

    ok = 0
    with httpx.Client(timeout=args.timeout) as client:
        requests = build_activation_requests(
            base_url=args.base_url,
            access_token=token,
        )
        for req in requests:
            method = req["method"]
            path = req["path"]
            try:
                resp = client.request(method, req["url"], headers=req["headers"])
                result = f"{resp.status_code}"
                try:
                    result += " " + _summarize_json(resp.json())
                except Exception:
                    snippet = resp.text[:120].replace("\n", " ")
                    result += f" text={snippet!r}"
                if resp.status_code >= 400:
                    result += " | " + _extract_error_details(resp)
                if 200 <= resp.status_code < 400:
                    ok += 1
            except Exception as exc:
                result = f"ERR {exc.__class__.__name__}: {exc}"
            print(f"{method:<6} {path:<56} {result[:5]:<5} {result}")

    print("-" * 110)
    print(f"Successful endpoints: {ok}/{len(requests)}")
    if ok != len(requests):
        sys.exit(2)


if __name__ == "__main__":
    main()
