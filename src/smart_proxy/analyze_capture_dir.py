"""Summarize raw capture files written by anthropic-debug-proxy (meta only — no secrets)."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) < 1:
        print("Usage: python -m smart_proxy analyze-capture-dir <directory>", file=sys.stderr)
        sys.exit(1)
    root = Path(argv[0]).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)
    metas = sorted(root.glob("*_meta.json"))
    if not metas:
        print(f"No *_meta.json under {root}")
        return
    for p in metas:
        data = json.loads(p.read_text(encoding="utf-8"))
        print("---")
        print(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\nTotal captures: {len(metas)}")
    print("Full headers/bodies: *_request_headers.json, *_request_body.bin, *_response_*.bin")


if __name__ == "__main__":
    main()
