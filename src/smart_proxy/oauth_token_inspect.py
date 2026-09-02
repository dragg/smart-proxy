"""Inspect Anthropic OAuth bearer tokens: JWT payload (exp/iat) vs opaque string."""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone


def _decode_jwt_payload(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    pad = (4 - len(payload_b64) % 4) % 4
    payload_b64 += "=" * pad
    raw = base64.urlsafe_b64decode(payload_b64)
    obj = json.loads(raw)
    return obj if isinstance(obj, dict) else None


def analyze_bearer_value(raw: str) -> str:
    """Return one human-readable line (no full secret)."""
    v = raw.strip()
    if v.lower().startswith("bearer "):
        v = v[7:].strip()

    if not v:
        return "empty token"

    # Classic JWT: three Base64URL segments
    if v.count(".") == 2:
        try:
            payload = _decode_jwt_payload(v)
            if payload is None:
                return "JWT-shaped but payload not a JSON object"
            lines: list[str] = ["format=jwt"]
            now = time.time()
            for key in ("exp", "iat", "nbf", "auth_time"):
                if key not in payload:
                    continue
                val = payload[key]
                if isinstance(val, (int, float)):
                    dt = datetime.fromtimestamp(float(val), tz=timezone.utc)
                    if key == "exp":
                        rem = float(val) - now
                        lines.append(
                            f"exp={dt.isoformat()} (unix={int(val)}) "
                            f"remaining≈{rem / 3600:.2f}h"
                        )
                    else:
                        lines.append(f"{key}={dt.isoformat()} (unix={int(val)})")
            # other useful claims without dumping everything
            for key in ("scope", "sub", "aud", "iss"):
                if key in payload:
                    lines.append(f"{key}={payload[key]!r}")
            return " | ".join(lines)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            return f"JWT-shaped but decode error: {e}"

    # Opaque (e.g. sk-ant-oat01-…): no exp/iat inside the string — TTL only from server / refresh
    short = f"{v[:12]}…" if len(v) > 12 else v
    return (
        f"format=opaque len={len(v)} start={short!r} "
        f"(not a 3-segment JWT — no embedded expiry; use refresh or 401 from API)"
    )


def main(argv: list[str] | None = None) -> None:
    import sys

    if argv is None:
        argv = sys.argv[1:]
    if len(argv) >= 1:
        raw = argv[0]
    else:
        raw = sys.stdin.read()
    print(analyze_bearer_value(raw))


if __name__ == "__main__":
    main()
