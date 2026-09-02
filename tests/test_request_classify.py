from __future__ import annotations
import sys
import unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from smart_proxy.request_classify import classify_request, RequestClass, MAIN_SYSTEM_MIN_CHARS


def _big(n): return "x" * n


class TestClassify:
    def test_no_tools_is_helper(self):
        rc = classify_request({"tools": [], "system": _big(50000)})
        assert rc.kind == "helper"

    def test_tools_plus_big_system_is_main(self):
        body = {"tools": [{"name": "Bash"}], "system": _big(MAIN_SYSTEM_MIN_CHARS + 1)}
        assert classify_request(body).kind == "main"

    def test_tools_plus_small_system_is_subagent(self):
        body = {"tools": [{"name": "Read"}], "system": _big(3000)}
        assert classify_request(body).kind == "subagent"

    def test_billing_block_excluded_from_system_size(self):
        # A tiny task system + a billing block must still classify as subagent.
        system = [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=1; " + _big(40000)},
            {"type": "text", "text": _big(3000)},
        ]
        body = {"tools": [{"name": "Read"}], "system": system}
        assert classify_request(body).kind == "subagent"

    def test_session_id_parsed_from_user_id_json(self):
        meta = {"user_id": '{"device_id":"d","account_uuid":"","session_id":"sess-123"}'}
        rc = classify_request({"tools": [{"name": "Bash"}], "system": _big(30000), "metadata": meta})
        assert rc.session_id == "sess-123"

    def test_session_id_absent_is_empty(self):
        assert classify_request({"tools": [], "metadata": {}}).session_id == ""
        assert classify_request({"tools": []}).session_id == ""

    def test_entrypoint_from_x_app_then_user_agent(self):
        assert classify_request({}, x_app="cli").entrypoint == "cli"
        assert classify_request({}, user_agent="claude-cli/2.1.92 (external, cli)").entrypoint == "cli"
        assert classify_request({}).entrypoint == ""

    def test_non_dict_body_is_helper(self):
        assert classify_request(None).kind == "helper"  # type: ignore[arg-type]


class ProjectAndTitleTests(unittest.TestCase):
    def _main_body(self, system, messages):
        # ≥15000-char system + tools ⇒ classified as "main"
        return {"system": system, "messages": messages, "tools": [{"name": "x"}]}

    def test_project_basenames_last_two_path_components(self):
        from smart_proxy.request_classify import _project
        sys_text = "x" * 100 + "\n# Environment\n - Primary working directory: /Users/n/Projects/Acme/api\n - more"
        self.assertEqual(_project(sys_text), "Acme/api")

    def test_project_handles_paths_with_spaces_and_list_system(self):
        from smart_proxy.request_classify import _project
        system = [{"type": "text", "text": "Primary working directory: /Users/n/My Projects/smart-proxy"}]
        self.assertEqual(_project(system), "My Projects/smart-proxy")

    def test_project_absent_returns_empty(self):
        from smart_proxy.request_classify import _project
        self.assertEqual(_project("no directory here"), "")
        self.assertEqual(_project(None), "")

    def test_snippet_skips_wrappers_and_finds_human_text(self):
        from smart_proxy.request_classify import _title_snippet
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "<system-reminder>\nboot hook</system-reminder>"}]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "починить баг в логине"},
        ]
        self.assertEqual(_title_snippet(messages), "починить баг в логине")

    def test_snippet_skips_compaction_and_interrupt_preambles(self):
        from smart_proxy.request_classify import _title_snippet
        messages = [
            {"role": "user", "content": "This session is being continued from a previous conversation..."},
            {"role": "user", "content": "[Request interrupted by user]"},
            {"role": "user", "content": "add a logout button"},
        ]
        self.assertEqual(_title_snippet(messages), "add a logout button")

    def test_snippet_ignores_tool_result_blocks(self):
        from smart_proxy.request_classify import _title_snippet
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "content": "SECRET OUTPUT"}]},
            {"role": "user", "content": [{"type": "text", "text": "real ask"}]},
        ]
        self.assertEqual(_title_snippet(messages), "real ask")

    def test_snippet_truncates_to_140(self):
        from smart_proxy.request_classify import _title_snippet
        long = "a" * 300
        self.assertEqual(len(_title_snippet([{"role": "user", "content": long}])), 140)

    def test_snippet_defensive_on_garbage(self):
        from smart_proxy.request_classify import _title_snippet
        self.assertEqual(_title_snippet(None), "")
        self.assertEqual(_title_snippet([{"role": "user"}]), "")
        self.assertEqual(_title_snippet("not a list"), "")

    def test_classify_fills_label_only_for_main(self):
        from smart_proxy.request_classify import classify_request
        big = "Primary working directory: /a/smart-proxy\n" + ("z" * 20000)
        main = classify_request(self._main_body(big, [{"role": "user", "content": "hello there"}]))
        self.assertEqual(main.kind, "main")
        self.assertEqual(main.project, "a/smart-proxy")
        self.assertEqual(main.title, "hello there")
        # subagent: tools present but small system ⇒ no label
        sub = classify_request({"system": "Primary working directory: /a/smart-proxy", "tools": [{"name": "x"}],
                                "messages": [{"role": "user", "content": "do a subtask"}]})
        self.assertEqual(sub.kind, "subagent")
        self.assertEqual((sub.project, sub.title), ("", ""))
        # helper: no tools ⇒ no label
        helper = classify_request({"system": "s", "messages": [{"role": "user", "content": "ping"}]})
        self.assertEqual(helper.kind, "helper")
        self.assertEqual((helper.project, helper.title), ("", ""))
