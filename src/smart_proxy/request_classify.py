"""Heuristic classification of Anthropic proxy requests.

Empirically (captured Claude Code traffic): the main agent loop sends a
~27 KB system prompt with the full toolset; a subagent (Task/Agent) shares
the same session_id but sends a stripped ~3-4 KB system prompt; background
helper calls (titles/summaries/quota) carry no tools. This is a heuristic,
not a guaranteed API flag, and the threshold may need re-tuning across
Claude Code versions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Main-loop system prompt ≈ 27 KB; subagent ≈ 3-4 KB. Threshold sits well between.
MAIN_SYSTEM_MIN_CHARS = 15000

_BILLING_PREFIX = "x-anthropic-billing"

_WORKDIR_RE = re.compile(r"Primary working directory:\s*(.+)")

# Prefixes that mark harness/context wrappers (not human-typed text). Includes
# the post-compaction and interrupt preambles, which are plain text — without
# them a compacted session's real title gets overwritten under last-write-wins.
_SNIPPET_SKIP_PREFIXES = (
    "<system-reminder", "<command-", "<local-command", "<persisted-output",
    "<user-", "Caveat:", "This session is being continued", "[Request interrupted",
)


@dataclass(frozen=True)
class RequestClass:
    kind: str          # "helper" | "main" | "subagent"
    session_id: str    # "" if unknown
    entrypoint: str    # "cli" | "sdk-cli" | "" ...
    project: str = ""  # folder label from the main system prompt; "" otherwise
    title: str = ""    # best-effort human-prompt snippet (≤140 chars); "" otherwise


def _system_text_len(system: object) -> int:
    """Total length of the request's system prompt, excluding the injected
    billing header block (which is not part of the agent instructions)."""
    if isinstance(system, str):
        return len(system)
    if isinstance(system, list):
        total = 0
        for block in system:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str) and not text.startswith(_BILLING_PREFIX):
                    total += len(text)
        return total
    return 0


def _system_text(system: object) -> str:
    """System prompt text (str or list of blocks), excluding the billing block."""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str) and not text.startswith(_BILLING_PREFIX):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _project(system: object) -> str:
    """Last one or two path components of 'Primary working directory: <path>'
    in the (main) system prompt. '' when absent. Never raises."""
    m = _WORKDIR_RE.search(_system_text(system))
    if not m:
        return ""
    path = m.group(1).strip().rstrip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    return "/".join(parts[-2:])


def _title_snippet(messages: object) -> str:
    """First genuine human-typed user text (wrappers/tool_results skipped),
    whitespace-collapsed, truncated to 140 chars. '' otherwise. Never raises."""
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        texts.append(t)
        for t in texts:
            stripped = t.strip()
            if not stripped or any(stripped.startswith(p) for p in _SNIPPET_SKIP_PREFIXES):
                continue
            return " ".join(stripped.split())[:140]
    return ""


def _session_id(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    uid = metadata.get("user_id")
    if not isinstance(uid, str):
        return ""
    try:
        obj = json.loads(uid)
    except Exception:
        return ""
    if isinstance(obj, dict):
        sid = obj.get("session_id")
        if isinstance(sid, str):
            return sid
    return ""


def _entrypoint(user_agent: str, x_app: str) -> str:
    if x_app.strip():
        return x_app.strip()
    ua = (user_agent or "").lower()
    if "claude-cli" in ua:
        return "cli"
    return ""


def classify_request(body: dict, *, user_agent: str = "", x_app: str = "") -> RequestClass:
    entrypoint = _entrypoint(user_agent, x_app)
    if not isinstance(body, dict):
        return RequestClass("helper", "", entrypoint)
    tools = body.get("tools")
    has_tools = isinstance(tools, list) and len(tools) > 0
    if not has_tools:
        kind = "helper"
    elif _system_text_len(body.get("system")) >= MAIN_SYSTEM_MIN_CHARS:
        kind = "main"
    else:
        kind = "subagent"
    project = title = ""
    if kind == "main":
        project = _project(body.get("system"))
        title = _title_snippet(body.get("messages"))
    return RequestClass(kind, _session_id(body.get("metadata")), entrypoint, project, title)
