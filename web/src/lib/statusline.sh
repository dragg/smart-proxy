#!/usr/bin/env bash
INPUT=$(cat)
echo "$INPUT" | python3 -c '
import sys, json, subprocess, os, time
from datetime import datetime, timezone

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

parts = []

# Rate limits — only when ANTHROPIC_PROXY_USAGE=true is set
if os.environ.get("ANTHROPIC_PROXY_USAGE") == "true":
    CACHE = "/tmp/claude_usage_cache.json"
    now = time.time()
    try:
        cache_age = now - os.path.getmtime(CACHE)
    except:
        cache_age = 9999

    if cache_age > 60:
        try:
            result = subprocess.run(
                ["curl", "-s", "--max-time", "3", "__PROXY_ORIGIN__/_oauth_usage?key=__PROXY_KEY__"],
                capture_output=True, text=True
            )
            with open(CACHE, "w") as f:
                f.write(result.stdout)
            cache_data = json.loads(result.stdout)
        except:
            cache_data = None
    else:
        try:
            with open(CACHE) as f:
                cache_data = json.load(f)
        except:
            cache_data = None

    if cache_data:
        try:
            key = cache_data["keys"][0]
            if key.get("http_status") == 200:
                usage = key["usage"]
                now_dt = datetime.now(timezone.utc)

                def fmt_reset(iso):
                    try:
                        dt = datetime.fromisoformat(iso)
                        secs = max(0, (dt - now_dt).total_seconds())
                        h, m = int(secs // 3600), int((secs % 3600) // 60)
                        if h >= 24:
                            return f"{h//24}d{h%24}h"
                        return f"{h}h{m:02d}m"
                    except:
                        return "?"

                # A window is null when it has no activity yet (e.g. no requests
                # in the current 5h window) -- show it as idle, keep the rest.
                for label, win_key in (("5h", "five_hour"), ("7d", "seven_day")):
                    win = usage.get(win_key)
                    if not win or win.get("utilization") is None:
                        parts.append(f"{label}: —")
                        continue
                    pct = round(win["utilization"])
                    reset = fmt_reset(win["resets_at"]) if win.get("resets_at") else "?"
                    parts.append(f"{label}: {pct}% (↺ {reset})")

                for lim in usage.get("limits") or []:
                    kind = lim.get("kind")
                    lim_reset = fmt_reset(lim["resets_at"]) if lim.get("resets_at") else "?"
                    if kind == "smartproxy_daily_usd":
                        pct = round(lim.get("percent") or 0)
                        spent = lim.get("spent_usd") or 0
                        cap = lim.get("limit_usd") or 0
                        parts.append(f"24h: {pct}% (${spent:.2f}/${cap:.2f} ↺ {lim_reset})")
                        continue
                    if kind != "weekly_scoped" or lim.get("percent") is None:
                        continue
                    scope = lim.get("scope") or {}
                    name = ((scope.get("model") or {}).get("display_name")
                            or scope.get("surface") or "scoped")
                    lim_pct = round(lim["percent"])
                    parts.append(f"{name} 7d: {lim_pct}% (↺ {lim_reset})")
            else:
                parts.append("5h:?%")
                parts.append("7d:?%")
        except:
            pass

# Context window usage
cw = data.get("context_window") or {}
if os.environ.get("ANTHROPIC_PROXY_USAGE") == "true":
    pct = cw.get("used_percentage") or 0
    window_size = cw.get("context_window_size") or 200000
    actual_tokens = pct / 100 * window_size
    ctx = round(actual_tokens / 200000 * 100)
else:
    ctx = cw.get("used_percentage")
if ctx is not None:
    parts.append(f"ctx:{round(ctx)}%")

# Model
model = (data.get("model") or {}).get("display_name") or ""
if model:
    if os.environ.get("ANTHROPIC_PROXY_USAGE") == "true":
        model = model.replace(" (1M context)", "")
    parts.append(model)

# Git branch + worktree indicator (linked worktree has git-dir != git-common-dir)
cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd")
if cwd and os.path.isdir(cwd):
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--path-format=absolute",
             "--abbrev-ref", "HEAD", "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, timeout=2)
        lines = r.stdout.splitlines()
        if r.returncode == 0 and len(lines) >= 3:
            branch = "detached" if lines[0] == "HEAD" else lines[0]
            wt = " (wt)" if os.path.realpath(lines[1]) != os.path.realpath(lines[2]) else ""
            parts.append(f"⎇ {branch}{wt}")
    except:
        pass

# Session cost
cost = (data.get("cost") or {}).get("total_cost_usd")
if cost is not None and cost > 0:
    parts.append("$" + f"{cost:.4f}")

# Prompt cache freshness: TTL counts from the last API request (any request in
# the session refreshes it, not just user prompts). Use the timestamp of the last
# transcript entry — NOT file mtime: an idle-but-open session keeps rewriting
# trailing metadata lines, so mtime stays fresh forever.
# 5m with API-key auth; 1h if ENABLE_PROMPT_CACHING_1H=1 (or subscription auth)
CACHE_TTL = 3600 if os.environ.get("ENABLE_PROMPT_CACHING_1H") in ("1", "true") else 300
def fmt_dur(secs):
    secs = int(secs)
    if secs >= 3600:
        return f"{secs//3600}h{(secs%3600)//60:02d}m"
    if secs >= 60:
        return f"{secs//60}m{secs%60:02d}s"
    return f"{secs}s"
def last_activity(path):
    import re
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        # a single JSONL line (big tool result) can exceed the first chunk
        for chunk_size in (65536, 4 * 1024 * 1024):
            f.seek(max(0, size - chunk_size))
            text = f.read().decode("utf-8", errors="replace")
            ts_list = re.findall(r"\"timestamp\"\s*:\s*\"([0-9T:.+Z-]+)\"", text)
            if ts_list:
                dt = datetime.fromisoformat(max(ts_list).replace("Z", "+00:00"))
                return dt.timestamp()
    return os.path.getmtime(path)
tp = data.get("transcript_path")
if tp and os.path.exists(tp):
    try:
        idle = time.time() - last_activity(tp)
        left = CACHE_TTL - idle
        if left > 0:
            parts.append(f"cache {fmt_dur(left)}")
        else:
            parts.append(f"cache ✗ ({fmt_dur(idle)} ago)")
    except:
        pass

# Age of last usage-data fetch
try:
    age = time.time() - os.path.getmtime("/tmp/claude_usage_cache.json")
    parts.append(f"upd {fmt_dur(age)} ago")
except:
    pass

print("  |  ".join(parts))
'
