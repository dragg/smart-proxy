"""Claude Code version identity: parsing, rendering, and learning from traffic."""

from __future__ import annotations

import unittest

from smart_proxy.claude_code_identity import (
    ClaudeCodeVersion,
    parse_version,
    render_billing_header,
    render_cli_user_agent,
    render_code_user_agent,
)


def _cli_body(cc_version: str, *, extra_first: str | None = None) -> dict:
    """A request body shaped like the one the real Claude Code CLI sends."""
    system = [{"type": "text", "text": f"x-anthropic-billing-header: cc_version={cc_version}; cc_entrypoint=sdk-cli;"}]
    if extra_first is not None:
        system.insert(0, {"type": "text", "text": extra_first})
    system.append({"type": "text", "text": "You are a Claude agent."})
    return {"model": "claude-fable-5-1", "system": system}


CLI_UA = "claude-cli/2.1.288 (external, sdk-cli)"


class ParseVersionTests(unittest.TestCase):
    def test_accepts_plain_triple(self) -> None:
        self.assertEqual(parse_version("2.1.260"), (2, 1, 260))

    def test_accepts_build_suffix(self) -> None:
        # The real CLI sends cc_version=2.1.260.222; the proxy's own template
        # uses .a35. Both are opaque build ids hanging off the same triple.
        self.assertEqual(parse_version("2.1.260.222"), (2, 1, 260))
        self.assertEqual(parse_version("2.1.260.a35"), (2, 1, 260))

    def test_rejects_malformed(self) -> None:
        for bad in ("2.1", "2.1.x", "2.1.260-beta", "", "x" * 500, "2.1.260.", "-1.0.0"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_version(bad))


class RenderTests(unittest.TestCase):
    def test_billing_header_carries_full_token(self) -> None:
        self.assertEqual(
            render_billing_header("2.1.260.a35"),
            "x-anthropic-billing-header: cc_version=2.1.260.a35; cc_entrypoint=sdk-cli; cch=00000;",
        )

    def test_user_agents_carry_triple_only(self) -> None:
        self.assertEqual(render_cli_user_agent("2.1.260.222"), "claude-cli/2.1.260 (external, sdk-cli)")
        self.assertEqual(render_code_user_agent("2.1.260.222"), "claude-code/2.1.260")

    def test_user_agent_entrypoint_agrees_with_billing_block(self) -> None:
        # The proxy used to claim `(external, cli)` in the UA while its billing
        # block said `cc_entrypoint=sdk-cli` — a pairing no real client sends.
        self.assertIn("sdk-cli", render_cli_user_agent("2.1.260"))
        self.assertIn("cc_entrypoint=sdk-cli", render_billing_header("2.1.260"))


class FloorTests(unittest.TestCase):
    def test_floor_without_suffix_gets_default_build_id(self) -> None:
        v = ClaudeCodeVersion("2.1.260")
        self.assertEqual(v.token, "2.1.260.a35")

    def test_floor_with_suffix_is_kept_verbatim(self) -> None:
        self.assertEqual(ClaudeCodeVersion("2.1.260.222").token, "2.1.260.222")

    def test_invalid_floor_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            ClaudeCodeVersion("not-a-version")


class LearnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.v = ClaudeCodeVersion("2.1.260")

    def _learn(self, **kw) -> str | None:
        return self.v.candidate(**kw)

    def test_learns_newer_version_from_real_cli_request(self) -> None:
        cand = self._learn(user_agent=CLI_UA, req_body=_cli_body("2.1.288.777"))
        self.assertEqual(cand, "2.1.288.777")
        self.assertTrue(self.v.commit(cand))
        self.assertEqual(self.v.token, "2.1.288.777")

    def test_candidate_alone_does_not_change_effective_version(self) -> None:
        # Learning is two-phase: nothing is adopted until upstream returns 2xx.
        self._learn(user_agent=CLI_UA, req_body=_cli_body("2.1.288.777"))
        self.assertEqual(self.v.token, "2.1.260.a35")

    def test_never_learns_downward(self) -> None:
        older = "claude-cli/2.1.92 (external, cli)"
        self.assertIsNone(self._learn(user_agent=older, req_body=_cli_body("2.1.92.a35")))

    def test_keeps_maximum_not_latest(self) -> None:
        self.v.commit(self.v.candidate(user_agent=CLI_UA, req_body=_cli_body("2.1.288.777")))
        older = "claude-cli/2.1.270 (external, sdk-cli)"
        self.assertIsNone(self._learn(user_agent=older, req_body=_cli_body("2.1.270.1")))
        self.assertEqual(self.v.token, "2.1.288.777")

    def test_major_bump_is_learned(self) -> None:
        # A numeric clamp keyed on minor would have frozen the proxy here — the
        # exact moment a new model gate is most likely to move.
        ua = "claude-cli/3.0.0 (external, sdk-cli)"
        self.assertEqual(self._learn(user_agent=ua, req_body=_cli_body("3.0.0.1")), "3.0.0.1")

    def test_suffix_only_change_is_not_learned(self) -> None:
        # Rewriting the block for a build-id churn would evict every cached
        # prefix for no gain: the gate reads the triple.
        ua = "claude-cli/2.1.260 (external, sdk-cli)"
        self.assertIsNone(self._learn(user_agent=ua, req_body=_cli_body("2.1.260.999")))

    def test_ignores_non_cli_user_agent(self) -> None:
        for ua in ("Anthropic/Python 0.80.0", "smart-proxy-openai-compat/1.0", ""):
            with self.subTest(ua=ua):
                self.assertIsNone(self._learn(user_agent=ua, req_body=_cli_body("2.1.288.777")))

    def test_requires_ua_and_block_to_agree(self) -> None:
        # The real CLI states its version twice; a mismatch means a forged half.
        self.assertIsNone(self._learn(user_agent=CLI_UA, req_body=_cli_body("2.1.999.1")))

    def test_only_reads_the_first_system_block(self) -> None:
        body = _cli_body("2.1.288.777", extra_first="You are a helpful assistant.")
        self.assertIsNone(self._learn(user_agent=CLI_UA, req_body=body))

    def test_ignores_mention_of_the_header_in_prose(self) -> None:
        body = {"system": [{"type": "text", "text": "Never send x-anthropic-billing-header: cc_version=9.9.9 anywhere."}]}
        self.assertIsNone(self._learn(user_agent="claude-cli/9.9.9 (external, cli)", req_body=body))

    def test_malformed_input_is_rejected_without_raising(self) -> None:
        for body in (None, {}, {"system": "plain string"}, {"system": []}, {"system": [None]},
                     _cli_body("2.1"), _cli_body("x" * 500), _cli_body("2.1.260-beta")):
            with self.subTest(body=body):
                self.assertIsNone(self._learn(user_agent=CLI_UA, req_body=body))

    def test_does_not_mutate_the_request_body(self) -> None:
        body = _cli_body("2.1.288.777")
        before = repr(body)
        self._learn(user_agent=CLI_UA, req_body=body)
        self.assertEqual(repr(body), before)

    def test_autolearn_disabled_pins_to_floor(self) -> None:
        v = ClaudeCodeVersion("2.1.260", autolearn=False)
        self.assertIsNone(v.candidate(user_agent=CLI_UA, req_body=_cli_body("2.1.288.777")))
        # Even a forced commit is refused, so an operator lowering the floor wins.
        self.assertFalse(v.commit("2.1.288.777"))
        self.assertEqual(v.token, "2.1.260.a35")

    def test_commit_revalidates(self) -> None:
        self.assertFalse(self.v.commit("2.1.92.a35"))   # stale by the time it lands
        self.assertFalse(self.v.commit("garbage"))
        self.assertFalse(self.v.commit(None))
        self.assertEqual(self.v.token, "2.1.260.a35")

    def test_reset_drops_learned_value(self) -> None:
        self.v.commit(self.v.candidate(user_agent=CLI_UA, req_body=_cli_body("2.1.288.777")))
        self.v.reset()
        self.assertEqual(self.v.token, "2.1.260.a35")


if __name__ == "__main__":
    unittest.main()
