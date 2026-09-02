"""mitmproxy addon: capture all requests to claude.com / anthropic.com domains."""

import json
import os
import time
from mitmproxy import http

CAPTURE_DIR = "./anthropic-proxy-test/captures"
DOMAINS = ("claude.com", "anthropic.com")
_counter = 0


def _matches(url: str) -> bool:
    return any(d in url for d in DOMAINS)


def request(flow: http.HTTPFlow) -> None:
    if not _matches(flow.request.pretty_url):
        return

    global _counter
    _counter += 1

    ts = time.strftime("%Y%m%d-%H%M%S")
    prefix = f"{CAPTURE_DIR}/all-{ts}-{_counter:04d}"
    os.makedirs(prefix, exist_ok=True)

    headers = dict(flow.request.headers)
    body_raw = flow.request.get_text()
    try:
        body = json.loads(body_raw)
    except Exception:
        body = body_raw

    with open(f"{prefix}/request.json", "w") as f:
        json.dump({
            "method": flow.request.method,
            "url": flow.request.pretty_url,
            "headers": headers,
            "body": body,
        }, f, indent=2)

    print(f"[{_counter:04d}] >>> {flow.request.method} {flow.request.pretty_url}")
    for k, v in headers.items():
        print(f"       {k}: {v[:80]}")
    if body:
        preview = json.dumps(body, ensure_ascii=False)[:200] if isinstance(body, dict) else str(body)[:200]
        print(f"       BODY: {preview}")
    print()

    flow.metadata["capture_prefix"] = prefix


def response(flow: http.HTTPFlow) -> None:
    prefix = flow.metadata.get("capture_prefix")
    if not prefix:
        return

    resp_headers = dict(flow.response.headers)
    resp_body_raw = flow.response.get_text()
    try:
        resp_body = json.loads(resp_body_raw)
    except Exception:
        resp_body = resp_body_raw

    with open(f"{prefix}/response.json", "w") as f:
        json.dump({
            "status": flow.response.status_code,
            "headers": resp_headers,
            "body": resp_body,
        }, f, indent=2)

    safe_body = ""
    if isinstance(resp_body, dict):
        safe = {}
        for k, v in resp_body.items():
            if isinstance(v, str) and len(v) > 30:
                safe[k] = v[:30] + "..."
            else:
                safe[k] = v
        safe_body = json.dumps(safe, ensure_ascii=False)[:300]
    else:
        safe_body = str(resp_body)[:300]

    print(f"       <<< {flow.response.status_code}  {safe_body}")
    for k, v in resp_headers.items():
        if k.lower() in ("retry-after", "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests",
                          "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
                          "anthropic-ratelimit-requests-limit", "anthropic-ratelimit-requests-remaining",
                          "anthropic-ratelimit-tokens-limit", "anthropic-ratelimit-tokens-remaining"):
            print(f"       {k}: {v}")
    print()
