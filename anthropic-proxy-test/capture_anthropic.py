"""mitmproxy addon: capture & log all Anthropic / Claude traffic.

Usage:
    mitmdump -p 8082 --ssl-insecure -s capture_anthropic.py
"""

from __future__ import annotations

import json
import os
import time
from mitmproxy import http

CAPTURE_DIR = os.path.join(os.path.dirname(__file__), "captures")

DOMAINS = (
    "anthropic.com",
    "claude.com",
    "claude.ai",
)

TOKEN_PATTERNS = ("sk-ant-ort01-", "oauth/token", "refresh_token")

_MAX_BODY_LOG = 2000
_MAX_BODY_FULL = 50_000
_counter = 0

# ANSI colours
_RED = "\033[91m"
_YELLOW = "\033[93m"
_GREEN = "\033[92m"
_CYAN = "\033[96m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _matches_domain(url: str) -> bool:
    return any(d in url for d in DOMAINS)


def _is_token_related(url: str, body: str | None) -> bool:
    text = (url + " " + (body or "")).lower()
    return any(p in text for p in TOKEN_PATTERNS)


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"... [{len(s)} total chars]"


def _safe_json(body_raw: str | None) -> str | dict | None:
    if not body_raw:
        return None
    try:
        return json.loads(body_raw)
    except Exception:
        return body_raw


def _redact_tokens(obj: dict) -> dict:
    """Shorten long token values for console display."""
    out = {}
    for k, v in obj.items():
        if isinstance(v, str) and len(v) > 40 and ("token" in k.lower() or "key" in k.lower()):
            out[k] = v[:25] + "..." + v[-6:]
        elif isinstance(v, dict):
            out[k] = _redact_tokens(v)
        else:
            out[k] = v
    return out


def request(flow: http.HTTPFlow) -> None:
    url = flow.request.pretty_url
    body_text = flow.request.get_text()

    if not _matches_domain(url) and not _is_token_related(url, body_text):
        return

    global _counter
    _counter += 1
    ts = time.strftime("%Y%m%d-%H%M%S")
    seq = f"{_counter:04d}"

    capture_dir = os.path.join(CAPTURE_DIR, f"traffic-{ts}-{seq}")
    os.makedirs(capture_dir, exist_ok=True)
    flow.metadata["capture_dir"] = capture_dir
    flow.metadata["seq"] = seq

    headers = dict(flow.request.headers)
    body = _safe_json(body_text)

    is_token = _is_token_related(url, body_text)
    body_limit = _MAX_BODY_FULL if is_token else _MAX_BODY_LOG

    with open(os.path.join(capture_dir, "request.json"), "w") as f:
        json.dump({
            "seq": seq,
            "timestamp": ts,
            "method": flow.request.method,
            "url": url,
            "headers": headers,
            "body": body,
            "is_token_related": is_token,
        }, f, indent=2, default=str)

    colour = _YELLOW if is_token else _CYAN
    print(f"\n{colour}[{seq}] >>> {flow.request.method} {url}{_RESET}")
    for k, v in headers.items():
        print(f"  {_DIM}{k}: {_truncate(str(v), 100)}{_RESET}")
    if body:
        if isinstance(body, dict):
            display = json.dumps(_redact_tokens(body), ensure_ascii=False)
        else:
            display = str(body)
        print(f"  BODY: {_truncate(display, body_limit)}")


def response(flow: http.HTTPFlow) -> None:
    capture_dir = flow.metadata.get("capture_dir")
    if not capture_dir:
        return

    seq = flow.metadata.get("seq", "????")
    url = flow.request.pretty_url
    status = flow.response.status_code
    resp_headers = dict(flow.response.headers)
    resp_text = flow.response.get_text()
    resp_body = _safe_json(resp_text)

    is_token = flow.metadata.get("is_token_related") or _is_token_related(url, resp_text)
    body_limit = _MAX_BODY_FULL if is_token else _MAX_BODY_LOG

    with open(os.path.join(capture_dir, "response.json"), "w") as f:
        json.dump({
            "status": status,
            "headers": resp_headers,
            "body": resp_body,
        }, f, indent=2, default=str)

    if status >= 400:
        colour = _RED
    elif is_token:
        colour = _YELLOW
    else:
        colour = _GREEN

    print(f"{colour}[{seq}] <<< {status}{_RESET}")

    rate_headers = {
        k: v for k, v in resp_headers.items()
        if any(p in k.lower() for p in (
            "retry-after", "ratelimit", "x-ratelimit",
            "anthropic-ratelimit",
        ))
    }
    if rate_headers:
        for k, v in rate_headers.items():
            print(f"  {_RED}{k}: {v}{_RESET}")

    if resp_body:
        if isinstance(resp_body, dict):
            display = json.dumps(_redact_tokens(resp_body), ensure_ascii=False)
        else:
            display = str(resp_body)
        print(f"  {_truncate(display, body_limit)}")
