Raw capture (unredacted OAuth / API traffic)
============================================

WARNING: files contain live secrets. Directory is gitignored. Do not commit.

1) Terminal A — proxy with capture directory:

   cd <repo>
   export ANTHROPIC_DEBUG_PROXY_RAW_DIR="$PWD/anthropic-proxy-test/captures/manual-$(date +%Y%m%d-%H%M%S)"
   mkdir -p "$ANTHROPIC_DEBUG_PROXY_RAW_DIR"
   .venv/bin/python -m smart_proxy anthropic-debug-proxy

2) Terminal B — point Claude Code at the proxy, then log in.

   export ANTHROPIC_BASE_URL=http://127.0.0.1:8090
   cd <repo>/anthropic-proxy-test

   Create anthropic-proxy-test/.claude/settings.json (gitignored) so this shell
   uses only the capture proxy:

       {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8090"}}

   Load only that project file, not ~/.claude/settings.json with unrelated proxy keys.
   --setting-sources is a flag on the main `claude` command, NOT on `auth login`:

   claude --setting-sources project auth login

   (Browser OAuth may talk to claude.com directly; API calls to api.anthropic.com
   go through the proxy and are captured.)

3) Summarize captures (meta JSON only):

   .venv/bin/python -m smart_proxy analyze-capture-dir "$ANTHROPIC_DEBUG_PROXY_RAW_DIR"

4) Inspect full headers/tokens: open *_request_headers.json in that folder.

Env:
  ANTHROPIC_DEBUG_PROXY_PORT       default 8090
  ANTHROPIC_DEBUG_PROXY_RAW_DIR    required for raw dumps
  ANTHROPIC_DEBUG_PROXY_CAPTURE_MAX_MB  max response body stored (default 80)
