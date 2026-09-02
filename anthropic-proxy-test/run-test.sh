#!/usr/bin/env bash
# Run Claude Code through the local Anthropic debug proxy (smart-proxy anthropic-debug-proxy on :8090).
set -euo pipefail
cd "$(dirname "$0")"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://127.0.0.1:8090}"
exec claude -p "Reply with exactly one word: pong" \
  --dangerously-skip-permissions \
  --setting-sources project
