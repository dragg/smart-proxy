"""Single source of truth for the Claude Code fingerprint the proxy claims.

Anthropic gates newer models on a minimum Claude Code version, and it reads that
version from the ``x-anthropic-billing-header`` block a client puts first in its
system prompt — **not** from the User-Agent. Sending ``claude-cli/2.1.260`` while
the block says ``cc_version=2.1.92`` is still rejected; sending an SDK's own
User-Agent with a current block is accepted.

A real Claude Code CLI sends its own block, so it is never affected. Everything
else — SDK clients, the OpenAI-compat loopback, the OAuth smoke check — relies on
the block this proxy synthesizes. When that pinned version falls behind a gate,
those clients get a hard 400 ``claude_code_version_too_old`` on the gated model
while every other model keeps answering, which is how it goes unnoticed.

So the version is not pinned twice. It is configured once as a floor and then
learned from the real Claude Code traffic the proxy already carries
(:class:`ClaudeCodeVersion`), and every UA and billing block is rendered from
that one value.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Build id appended to a bare floor. Opaque to us; the real CLI ships a numeric
# one (``2.1.260.222``), this is the value the proxy has always sent and is the
# only one empirically confirmed to pass the gate on a non-CLI User-Agent.
_DEFAULT_BUILD_ID = "a35"

# Floor the proxy claims until real Claude Code traffic teaches it a newer one.
# Bump when Anthropic raises a gate and no CLI traffic is available to learn
# from; `npm view @anthropic-ai/claude-code version` is the authoritative source.
DEFAULT_CLAUDE_CODE_VERSION = "2.1.260"

# The proxy presents itself as the SDK entrypoint, so the User-Agent and the
# billing block must both say so — a `(external, cli)` UA paired with
# `cc_entrypoint=sdk-cli` is a combination no real client sends.
_ENTRYPOINT = "sdk-cli"

_VERSION_RE = re.compile(r"^(\d{1,3})\.(\d{1,4})\.(\d{1,5})(?:\.([0-9A-Za-z]{1,16}))?$")

# Anchored at the start of the block: `_has_billing` in the proxy matches the
# header name anywhere in any system block, which is fine for "don't inject
# twice" but far too loose to learn from — a prompt that merely mentions the
# header would teach the proxy a version.
_BILLING_PREFIX = "x-anthropic-billing-header:"
_CC_VERSION_RE = re.compile(r"(?:^|[\s;])cc_version=([^\s;]{1,64})")

_CLI_UA_RE = re.compile(r"^claude-cli/(\d{1,3}\.\d{1,4}\.\d{1,5})\b")


def parse_version(token: str | None) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` for a ``cc_version`` value, else ``None``.

    The optional fourth component is a build id and is deliberately dropped: the
    gate compares the triple, so churn there must not look like a new version.
    """
    if not isinstance(token, str):
        return None
    match = _VERSION_RE.match(token.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _triple_str(token: str) -> str:
    triple = parse_version(token)
    return "" if triple is None else "{}.{}.{}".format(*triple)


def render_billing_header(token: str) -> str:
    """The system block the gate reads. Carries the full token, build id included."""
    return (
        f"x-anthropic-billing-header: cc_version={token}; "
        f"cc_entrypoint={_ENTRYPOINT}; cch=00000;"
    )


def render_cli_user_agent(token: str) -> str:
    return f"claude-cli/{_triple_str(token)} (external, {_ENTRYPOINT})"


def render_code_user_agent(token: str) -> str:
    return f"claude-code/{_triple_str(token)}"


def _billing_token(req_body: object) -> str | None:
    """Extract ``cc_version`` from a request body's leading billing block.

    Deliberately strict — only ``system[0]``, only a dict text block, only when
    the text *starts* with the header name. Never mutates ``req_body``.
    """
    if not isinstance(req_body, dict):
        return None
    system = req_body.get("system")
    if not isinstance(system, list) or not system:
        return None
    first = system[0]
    if not isinstance(first, dict):
        return None
    text = first.get("text")
    if not isinstance(text, str) or not text.lstrip().lower().startswith(_BILLING_PREFIX):
        return None
    match = _CC_VERSION_RE.search(text)
    return match.group(1) if match is not None else None


class ClaudeCodeVersion:
    """The Claude Code version the proxy claims, learned from real CLI traffic.

    Learning is two-phase on purpose. :meth:`candidate` only *proposes* a version
    seen on an inbound request; :meth:`commit` adopts it once that same request
    came back 2xx. So a version is only ever adopted after Anthropic itself
    accepted it — which is a far better guard than any numeric clamp we could
    invent, and it cannot strand the proxy on a value upstream rejects.

    State is per-process and lives on the aiohttp app. A restart falls back to
    the configured floor, which is safe: the floor is a real version, and the
    only cost is one prompt-cache miss for clients that carry the proxy's block.
    """

    def __init__(self, floor: str, *, autolearn: bool = True) -> None:
        if parse_version(floor) is None:
            raise ValueError(
                f"Invalid Claude Code version {floor!r}; expected MAJOR.MINOR.PATCH "
                "with an optional build id, e.g. '2.1.260' or '2.1.260.222'."
            )
        token = floor.strip()
        self._floor = token if token.count(".") >= 3 else f"{token}.{_DEFAULT_BUILD_ID}"
        self._autolearn = autolearn
        self._learned: str | None = None

    @property
    def token(self) -> str:
        """The ``cc_version`` value to render into outbound requests."""
        return self._learned or self._floor

    @property
    def triple(self) -> tuple[int, int, int]:
        return parse_version(self.token)  # type: ignore[return-value]

    @property
    def learned(self) -> str | None:
        return self._learned

    def candidate(self, *, user_agent: str | None, req_body: object) -> str | None:
        """Propose a version from an inbound request, or ``None``.

        Requires a ``claude-cli/`` User-Agent *and* a leading billing block whose
        triple agrees with it — the real CLI states its version in both places,
        so a disagreement means one half was forged or synthesized.
        """
        if not self._autolearn:
            return None

        ua_match = _CLI_UA_RE.match((user_agent or "").strip())
        if ua_match is None:
            return None
        ua_triple = parse_version(ua_match.group(1))

        token = _billing_token(req_body)
        block_triple = parse_version(token)
        if block_triple is None or block_triple != ua_triple:
            return None

        # Strictly greater on the triple only: a build-id change is not a new
        # version, and rewriting the block for one would evict cached prefixes.
        return token if block_triple > self.triple else None

    def commit(self, candidate: str | None) -> bool:
        """Adopt a candidate that upstream has just accepted. Returns whether it took."""
        if not self._autolearn:
            return False
        triple = parse_version(candidate)
        if triple is None or triple <= self.triple:
            return False
        previous = self.token
        self._learned = candidate.strip()  # type: ignore[union-attr]
        logger.info(
            "Claude Code version learned from upstream-accepted traffic: %s -> %s",
            previous, self._learned,
        )
        return True

    def reset(self) -> None:
        """Drop the learned value, falling back to the configured floor."""
        if self._learned is not None:
            logger.info("Claude Code version reset to configured floor %s", self._floor)
        self._learned = None
