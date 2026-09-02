from __future__ import annotations
import sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from smart_proxy.anthropic_proxy import record_kwargs_for


def test_record_kwargs_for_main_includes_project_and_title():
    body = {
        "tools": [{"name": "x"}],
        "system": "Primary working directory: /a/smart-proxy\n" + ("z" * 20000),
        "messages": [{"role": "user", "content": "fix the login bug"}],
        "metadata": {"user_id": '{"session_id": "sess-9"}'},
    }
    kw = record_kwargs_for(body, {})
    assert kw == {"request_kind": "main", "session_id": "sess-9",
                  "project": "a/smart-proxy", "title": "fix the login bug"}


def test_record_kwargs_for_garbage_is_safe():
    assert record_kwargs_for(None, {}) == {
        "request_kind": "unknown", "session_id": "", "project": "", "title": ""}
