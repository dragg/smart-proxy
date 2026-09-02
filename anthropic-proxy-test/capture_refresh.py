"""mitmproxy addon: capture OAuth refresh requests to platform.claude.com."""

import json
import time
from mitmproxy import http

CAPTURE_DIR = "./anthropic-proxy-test/captures"

def request(flow: http.HTTPFlow) -> None:
    if "oauth/token" not in flow.request.pretty_url:
        return

    ts = time.strftime("%Y%m%d-%H%M%S")
    prefix = f"{CAPTURE_DIR}/refresh-{ts}"

    meta = {
        "method": flow.request.method,
        "url": flow.request.pretty_url,
        "timestamp": ts,
    }

    headers = dict(flow.request.headers)

    body_raw = flow.request.get_text()
    try:
        body = json.loads(body_raw)
    except Exception:
        body = body_raw

    import os
    os.makedirs(prefix, exist_ok=True)

    with open(f"{prefix}/meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    with open(f"{prefix}/request_headers.json", "w") as f:
        json.dump(headers, f, indent=2)

    with open(f"{prefix}/request_body.json", "w") as f:
        json.dump(body, f, indent=2)

    print(f"\n{'='*60}")
    print(f"CAPTURED REFRESH REQUEST → {flow.request.pretty_url}")
    print(f"Method: {flow.request.method}")
    print(f"Headers: {json.dumps(headers, indent=2)}")
    print(f"Body: {json.dumps(body, indent=2)}")
    print(f"Saved to: {prefix}/")
    print(f"{'='*60}\n")


def response(flow: http.HTTPFlow) -> None:
    if "oauth/token" not in flow.request.pretty_url:
        return

    ts = time.strftime("%Y%m%d-%H%M%S")

    resp_headers = dict(flow.response.headers)
    resp_body_raw = flow.response.get_text()
    try:
        resp_body = json.loads(resp_body_raw)
    except Exception:
        resp_body = resp_body_raw

    print(f"\n{'='*60}")
    print(f"REFRESH RESPONSE status={flow.response.status_code}")
    print(f"Headers: {json.dumps(resp_headers, indent=2)}")
    if isinstance(resp_body, dict):
        safe = {k: (v[:20] + "..." if isinstance(v, str) and len(v) > 20 else v) for k, v in resp_body.items()}
        print(f"Body: {json.dumps(safe, indent=2)}")
    else:
        print(f"Body: {str(resp_body)[:500]}")
    print(f"{'='*60}\n")

    # Save response too
    import glob, os
    dirs = sorted(glob.glob(f"{CAPTURE_DIR}/refresh-*"))
    if dirs:
        latest = dirs[-1]
        with open(f"{latest}/response_headers.json", "w") as f:
            json.dump(resp_headers, f, indent=2)
        with open(f"{latest}/response_body.json", "w") as f:
            json.dump(resp_body, f, indent=2)
        with open(f"{latest}/response_status.txt", "w") as f:
            f.write(str(flow.response.status_code))
