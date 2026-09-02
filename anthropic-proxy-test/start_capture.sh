#!/usr/bin/env bash
#
# Transparent capture of ALL Anthropic/Claude traffic using mitmproxy local mode.
#
# Uses macOS Network Extension to intercept traffic at system level —
# catches everything including Bun, Node.js, curl, etc. without proxy env vars.
#
# Usage:  ./start_capture.sh
#
# Ctrl-C to stop.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "  Anthropic/Claude Traffic Capture"
echo "  (mitmproxy local mode — system-wide)"
echo "=========================================="
echo ""
echo "This will intercept ALL network traffic on this machine"
echo "and log requests to Anthropic/Claude domains."
echo ""
echo "You may be prompted for your password (sudo for Network Extension)."
echo ""

# ---------------------------------------------------------------
# Start mitmproxy in local mode with capture addon
#
# --mode local        : transparent system-wide interception via macOS Network Extension
# --ssl-insecure      : don't verify upstream SSL (needed for some edge cases)
# --set connection_strategy=lazy : only connect upstream when data is sent (avoids noise)
# -s capture script   : our addon that filters & logs Anthropic/Claude traffic
# ---------------------------------------------------------------
sudo mitmdump \
    --mode local \
    --ssl-insecure \
    -s "$SCRIPT_DIR/capture_anthropic.py"
