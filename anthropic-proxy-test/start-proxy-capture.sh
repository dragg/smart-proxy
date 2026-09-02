#!/usr/bin/env bash
# Start anthropic-debug-proxy with ANTHROPIC_DEBUG_PROXY_RAW_DIR under ./captures/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_ID="${1:-run-$(date +%Y%m%d-%H%M%S)}"
export ANTHROPIC_DEBUG_PROXY_RAW_DIR="${ROOT}/anthropic-proxy-test/captures/${RUN_ID}"
mkdir -p "${ANTHROPIC_DEBUG_PROXY_RAW_DIR}"
echo "Captures will be written to: ${ANTHROPIC_DEBUG_PROXY_RAW_DIR}"
echo "In another terminal:"
echo "  export ANTHROPIC_BASE_URL=http://127.0.0.1:8090"
echo "  cd ${ROOT}/anthropic-proxy-test && claude --setting-sources project auth login"
exec "${ROOT}/.venv/bin/python" -m smart_proxy anthropic-debug-proxy
