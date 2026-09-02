# Primary/Standby Anthropic Key Failover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one OAuth key on a subscription act as a dormant hot standby — kept credential-alive with zero inference/usage footprint — that auto-promotes to serve the moment the active (primary) key is deactivated by a refresh failure.

**Architecture:** A `role` column (`primary`/`standby`) on `anthropic_keys`. The pool serves only primaries and falls to a standby exactly when no primary is alive (`status != "inactive"`; cooled/low_balance primaries stay "alive"). Serving a standby auto-promotes it (role→primary). Standbys are kept warm refresh-only via the daily smoke pass and are excluded from the smoke inference request and from the usage poller. Builds on the OAuth refresh durability fix already on main (`ensure_valid_token`/`_refresh_locked`).

**Tech Stack:** Python 3.11+, aiohttp, httpx, asyncio; SQLite (dev) + Postgres (prod); Svelte 5 dashboard (Vite). Tests: `unittest` + sqlite fallback DB (`connect_test_database`).

## Global Constraints

- Test command is **`uv run pytest`** (system python lacks aiohttp). Restore `git checkout -- uv.lock` if it drifts.
- `role` values are exactly `"primary" | "standby"`; DB default `'primary'`. Existing keys and all `pick()` behavior are unchanged until a key is set to standby (locked by a regression test).
- "Alive primary" = `role == "primary" and status != "inactive"`. Failover triggers only on **deactivation**, never on cooldown or `low_balance`.
- Promotion happens only in the proxy path, **after** `ensure_valid_token` returns a valid token and **before** forwarding; the in-memory role flip precedes the first `await` in `promote_to_primary`.
- A standby must incur **no** inference, activation, quota, or prompt-cache footprint: excluded from the smoke `/v1/messages` request and skipped entirely by `_build_oauth_usage_payload`.
- `expires_at` is epoch milliseconds. No new third-party deps. Full suite (`uv run pytest -q`) stays green; SPA builds (`npm run build` in `web/`).
- Audit events via `_record_anthropic_event` / the DB `audit_*` params; new event `role_change`.
- Spec: `docs/superpowers/specs/2026-07-21-anthropic-standby-key-failover-design.md`.

---

### Task 1: Schema + `role` plumbing (DB + model)

**Files:**
- Modify: `src/smart_proxy/db.py` (schema `:139`, `MIGRATIONS` `:272`, `insert_anthropic_key` `:1547`, new `set_anthropic_key_role` after `set_anthropic_key_name` `:1788`)
- Modify: `src/smart_proxy/db_migrations.py` (`POSTGRES_MIGRATIONS` `:5`)
- Modify: `src/smart_proxy/anthropic_proxy.py` (`_AnthropicKey` `:129`, `_anthropic_key_from_row` `:164`)
- Modify: `src/smart_proxy/dashboard_api.py` (`_api_anthropic_keys` payload, ~`:166`)
- Test: `tests/test_anthropic_key_rename_db.py` (sibling DB tests) and `tests/test_dashboard_anthropic_keys_api.py`

**Interfaces:**
- Produces: `anthropic_keys.role` column; `_AnthropicKey.role: str`; `_anthropic_key_from_row` sets it; `insert_anthropic_key(..., role="primary")`; `db.set_anthropic_key_role(key_id, role, *, audit_...) -> bool` (records a `role_change` event); `/api/anthropic/keys` payload includes `"role"`.

- [ ] **Step 1: Write failing DB test** — add to `tests/test_anthropic_key_rename_db.py` (or a new `tests/test_anthropic_key_role_db.py` following the same harness with `connect_test_database`):

```python
    def test_role_defaults_primary_and_set_role_records_event(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    await db.insert_anthropic_key(id="k1", key_type="oauth", name="k1")
                    row = await db.get_anthropic_key("k1")
                    self.assertEqual(row["role"], "primary")

                    await db.insert_anthropic_key(id="k2", key_type="oauth", name="k2", role="standby")
                    self.assertEqual((await db.get_anthropic_key("k2"))["role"], "standby")

                    ok = await db.set_anthropic_key_role(
                        "k1", "standby", audit_source="dashboard",
                        audit_event_type="role_change", audit_decision="set_standby")
                    self.assertTrue(ok)
                    self.assertEqual((await db.get_anthropic_key("k1"))["role"], "standby")
                    events = await db.list_anthropic_key_events("k1")
                    self.assertTrue(any(e["event_type"] == "role_change" for e in events))
                    self.assertFalse(await db.set_anthropic_key_role("missing", "standby"))
                finally:
                    await db.close()
        asyncio.run(run())
```
(Match the file's existing imports: `asyncio`, `tempfile`, `Path`, `connect_test_database`.)

- [ ] **Step 2: Run it, verify failure**

Run: `cd <repo> && uv run pytest tests/test_anthropic_key_rename_db.py -k role -v` (or the new file)
Expected: FAIL — `role` column/param and `set_anthropic_key_role` don't exist.

- [ ] **Step 3: Add the column to schema + migrations.**
In `src/smart_proxy/db.py` `CREATE TABLE IF NOT EXISTS anthropic_keys` (`:139`), add after `name`:
```
    role              TEXT    NOT NULL DEFAULT 'primary',
```
Append to the `MIGRATIONS` list (`:272`):
```python
    "ALTER TABLE anthropic_keys ADD COLUMN role TEXT NOT NULL DEFAULT 'primary'",
```
In `src/smart_proxy/db_migrations.py`, append a new `POSTGRES_MIGRATIONS` entry with a globally-unique name (the tuple has duplicate numeric prefixes — use `0006_anthropic_keys_role`):
```python
    (
        "0006_anthropic_keys_role",
        (
            "ALTER TABLE anthropic_keys ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'primary'",
        ),
    ),
```

- [ ] **Step 4: Thread `role` through insert + add `set_anthropic_key_role`.**
In `insert_anthropic_key` (`:1547`), add param `role: str = "primary"` (after `name`), add `role` to the INSERT column list and a `?` to VALUES, and `role` to the params tuple:
```python
        name: str = "",
        role: str = "primary",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """INSERT INTO anthropic_keys
               (id, key_type, status, api_key, access_token, refresh_token,
                client_id, expires_at, scopes, subscription_type,
                rate_limit_tier, name, role, created_at, updated_at)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                id, key_type, api_key, access_token, refresh_token,
                client_id, expires_at, scopes, subscription_type,
                rate_limit_tier, name, role, now, now,
            ),
        )
        await self.db.commit()
```
Add `set_anthropic_key_role` after `set_anthropic_key_name` (mirror it exactly, changing the column and snapshot/context):
```python
    async def set_anthropic_key_role(
        self,
        key_id: str,
        role: str,
        *,
        audit_op_id: str = "",
        audit_source: str = "",
        audit_event_type: str = "",
        audit_decision: str = "",
        audit_path: str = "",
        audit_model: str | None = None,
        audit_http_status: int | None = None,
        audit_request_id: str = "",
        audit_error_type: str = "",
        audit_error_message: str = "",
        audit_retry_after: int | None = None,
        audit_context: dict | None = None,
    ) -> bool:
        """Set an Anthropic key's role ('primary'|'standby'). Returns False if id missing."""
        previous = await self.get_anthropic_key(key_id)
        if previous is None:
            return False
        now = datetime.now(timezone.utc).isoformat()
        snapshot_id: int | None = None
        if audit_event_type:
            snapshot_id = await self.record_anthropic_key_snapshot(
                key_id=key_id, snapshot_kind="before_role_change",
                trigger_event_type=audit_event_type, row=previous, commit=False,
            )
        await self.db.execute(
            "UPDATE anthropic_keys SET role = ?, updated_at = ? WHERE id = ?",
            (role, now, key_id),
        )
        if audit_event_type:
            context = dict(audit_context or {})
            context.setdefault("previous_role", previous.get("role", "primary"))
            context.setdefault("next_role", role)
            await self.record_anthropic_key_event(
                key_id=key_id, event_type=audit_event_type, op_id=audit_op_id,
                source=audit_source, decision=audit_decision, path=audit_path,
                model=audit_model, http_status=audit_http_status, request_id=audit_request_id,
                error_type=audit_error_type, error_message=audit_error_message,
                retry_after=audit_retry_after, snapshot_id=snapshot_id, context=context, commit=False,
            )
        await self.db.commit()
        return True
```

- [ ] **Step 5: Add `role` to the model + row loader + dashboard payload.**
`src/smart_proxy/anthropic_proxy.py` `_AnthropicKey` (`:129`): add field `role: str = "primary"` (after `name`). `_anthropic_key_from_row` (`:164`): add `role=row.get("role") or "primary"`.
`src/smart_proxy/dashboard_api.py` `_api_anthropic_keys` payload dict: add `"role": r.get("role") or "primary",`.

- [ ] **Step 6: Add the dashboard payload test** — in `tests/test_dashboard_anthropic_keys_api.py`, extend `_key_row` to include `"role": "primary"` and add to `test_lists_keys_without_token_material`:
```python
        self.assertEqual(key["role"], "primary")
```

- [ ] **Step 7: Run, verify pass**

Run: `cd <repo> && uv run pytest tests/test_anthropic_key_rename_db.py tests/test_dashboard_anthropic_keys_api.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**
```bash
git add src/smart_proxy/db.py src/smart_proxy/db_migrations.py src/smart_proxy/anthropic_proxy.py src/smart_proxy/dashboard_api.py tests/test_anthropic_key_rename_db.py tests/test_dashboard_anthropic_keys_api.py
git commit -m "feat(keys): add role column (primary/standby) + set_anthropic_key_role

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Failover — pick tiering

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — `_find_pickable_oauth_index` (`:262`), `pick` (`:271`), `next_available_in` (`:342`), `deactivate` (`:377`), `mark_low_balance` (`:412`); add `_primary_alive` helper
- Test: `tests/test_proxy_key_active.py` (pool pick tests live here) or a new `tests/test_anthropic_standby_pick.py`

**Interfaces:**
- Consumes: `_AnthropicKey.role`, `.status` (Task 1).
- Produces: `pool._primary_alive() -> bool`; `pick()`/`next_available_in()` restricted to the eligible tier; `deactivate()`/`mark_low_balance()` update in-memory `key.status`.

- [ ] **Step 1: Write failing tests** — new `tests/test_anthropic_standby_pick.py`:

```python
from __future__ import annotations
import asyncio, sys, time, unittest
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path: sys.path.insert(0, str(SRC))
from smart_proxy.anthropic_proxy import AnthropicKeyPool, _AnthropicKey

def _key(kid, role="primary", status="active", key_type="oauth"):
    return _AnthropicKey(key_id=kid, key_type=key_type, status=status, api_key="k" if key_type=="api_key" else None,
                         access_token="a", refresh_token="r", client_id="c", expires_at=9_999_999_999_999,
                         scopes="[]", name=kid, role=role)

class StandbyPickTests(unittest.TestCase):
    def _pool(self, keys):
        p = AnthropicKeyPool(db=None)  # pick() never touches db
        p._keys = list(keys)
        return p

    def test_serves_primary_while_alive(self):
        p = self._pool([_key("prim"), _key("stby", role="standby")])
        self.assertEqual(p.pick().key_id, "prim")

    def test_cooled_primary_no_failover_and_retry_after(self):
        p = self._pool([_key("prim"), _key("stby", role="standby")])
        p.cooldown(p._keys[0], 120)
        self.assertIsNone(p.pick())                       # standby NOT served
        self.assertGreater(p.next_available_in(), 0)      # primary's cooldown, not 0 from standby

    def test_deactivated_primary_fails_over_to_standby(self):
        p = self._pool([_key("prim"), _key("stby", role="standby")])
        p._keys[0].status = "inactive"; p._banned.add("prim")
        self.assertEqual(p.pick().key_id, "stby")

    def test_low_balance_primary_no_failover(self):
        p = self._pool([_key("prim", status="low_balance"), _key("stby", role="standby")])
        p._banned.add("prim")                              # mark_low_balance bans
        self.assertIsNone(p.pick())                        # low_balance primary is still "alive" → no failover

    def test_api_key_primary_does_not_redirect_to_standby(self):
        p = self._pool([_key("apik", key_type="api_key"), _key("stby", role="standby")])
        self.assertEqual(p.pick().key_id, "apik")          # no cross-tier redirect

    def test_all_primary_unchanged(self):
        p = self._pool([_key("a"), _key("b")])
        self.assertEqual(p.pick().key_id, "a")             # sticky, identical to today

    def test_deactivate_and_low_balance_update_in_memory_status(self):
        async def run():
            calls = {}
            class _DB:
                async def set_anthropic_key_status(self, kid, status, **kw): calls[kid] = status
            p = AnthropicKeyPool(db=_DB()); p._keys = [_key("prim")]
            await p.deactivate(p._keys[0])
            self.assertEqual(p._keys[0].status, "inactive")
            p2 = AnthropicKeyPool(db=_DB()); p2._keys = [_key("prim")]
            await p2.mark_low_balance(p2._keys[0])
            self.assertEqual(p2._keys[0].status, "low_balance")
        asyncio.run(run())

if __name__ == "__main__": unittest.main()
```

- [ ] **Step 2: Run, verify failure**

Run: `cd <repo> && uv run pytest tests/test_anthropic_standby_pick.py -v`
Expected: FAIL (no tiering; standby served on cooldown; status not updated).

- [ ] **Step 3: Add `_primary_alive` + in-memory status updates.**
Add a method to `AnthropicKeyPool` (near `pick`):
```python
    def _primary_alive(self) -> bool:
        return any(k.role == "primary" and k.status != "inactive" for k in self._keys)
```
In `deactivate` (`:377`), right after `self._banned.add(key.key_id)`, add:
```python
        key.status = "inactive"
```
In `mark_low_balance` (`:412`), right after `self._banned.add(key.key_id)`, add:
```python
        key.status = "low_balance"
```

- [ ] **Step 4: Tier `_find_pickable_oauth_index`, `pick`, `next_available_in`.**
Replace `_find_pickable_oauth_index` (`:262`):
```python
    def _find_pickable_oauth_index(self, *, model: str | None, now: float, eligible_role: str) -> int | None:
        for index, key in enumerate(self._keys):
            if (key.role == eligible_role and key.key_type == "oauth"
                    and self._is_pickable(key, model=model, now=now)):
                return index
        return None
```
Replace the loop body in `pick` (`:283-295`):
```python
        eligible = "primary" if self._primary_alive() else "standby"
        for _ in range(n):
            current_index = self._index % n
            key = self._keys[current_index]
            if key.role != eligible or not self._is_pickable(key, model=model, now=now):
                self._index = (self._index + 1) % n
                continue
            if key.key_type == "api_key":
                oauth_index = self._find_pickable_oauth_index(model=model, now=now, eligible_role=eligible)
                if oauth_index is not None:
                    self._index = oauth_index
                    return self._keys[oauth_index]
            return key
        return None
```
(Insert `eligible = ...` right after `now = time.monotonic()`.)
In `next_available_in` (`:342`), compute the eligible tier and skip other-tier keys — add after `now = time.monotonic()`:
```python
        eligible = "primary" if self._primary_alive() else "standby"
```
and change the loop guard `if k.key_id in self._banned:` to:
```python
            if k.role != eligible or k.key_id in self._banned:
```

- [ ] **Step 5: Run, verify pass**

Run: `cd <repo> && uv run pytest tests/test_anthropic_standby_pick.py tests/test_proxy_key_active.py -q`
Expected: PASS (new tiering + existing pick tests green).

- [ ] **Step 6: Commit**
```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_standby_pick.py
git commit -m "feat(pool): tier pick/next_available_in by role; standby serves only when no primary alive

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Auto-promotion

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — add `promote_to_primary` (near `deactivate`); handler trigger after the `effective_token is None` block (`:1799-1806`)
- Test: `tests/test_anthropic_standby_pick.py` (promotion unit) + `tests/test_anthropic_proxy_oauth_messages.py` (handler e2e, if a fixture exists) or a focused pool test

**Interfaces:**
- Consumes: Task 1 `set_anthropic_key_role`, Task 2 tiering.
- Produces: `async pool.promote_to_primary(key, *, audit_...)` — flips `role` to `primary` (memory-first) and records `role_change`/`promote`; idempotent.

- [ ] **Step 1: Write failing test** — add to `tests/test_anthropic_standby_pick.py`:
```python
class PromotionTests(unittest.TestCase):
    def test_promote_flips_role_records_event_idempotent(self):
        async def run():
            calls = []
            class _DB:
                async def set_anthropic_key_role(self, kid, role, **kw):
                    calls.append((kid, role, kw.get("audit_decision"))); return True
            p = AnthropicKeyPool(db=_DB()); p._keys = [_key("stby", role="standby")]
            await p.promote_to_primary(p._keys[0], audit_source="proxy_request")
            self.assertEqual(p._keys[0].role, "primary")
            self.assertEqual(calls, [("stby", "primary", "promote")])
            await p.promote_to_primary(p._keys[0])          # idempotent: already primary
            self.assertEqual(len(calls), 1)
        asyncio.run(run())
```

- [ ] **Step 2: Run, verify failure**

Run: `cd <repo> && uv run pytest tests/test_anthropic_standby_pick.py -k Promotion -v`
Expected: FAIL — `promote_to_primary` undefined.

- [ ] **Step 3: Add `promote_to_primary`** to `AnthropicKeyPool` (near `deactivate`):
```python
    async def promote_to_primary(
        self, key: _AnthropicKey, *, audit_op_id: str = "", audit_source: str = "",
        audit_path: str = "", audit_model: str | None = None,
    ) -> None:
        """Promote a standby that is about to serve into the primary role. Idempotent."""
        if key.role != "standby":
            return
        key.role = "primary"  # memory-first (before await) — closes the idempotency window
        await self._db.set_anthropic_key_role(
            key.key_id, "primary",
            audit_op_id=audit_op_id, audit_source=audit_source,
            audit_event_type="role_change", audit_decision="promote",
            audit_path=audit_path, audit_model=audit_model,
            audit_error_message="Standby promoted to primary on failover",
        )
        logger.warning("Standby key %s promoted to primary", key.key_id[:12])
```

- [ ] **Step 4: Wire the handler trigger.** In `_proxy_handler`, immediately after the `if effective_token is None:` block ends (`:1799-1806`, the `continue`) and before the forwarding (`fwd = ...`), add:
```python
        if key.role == "standby":
            await pool.promote_to_primary(
                key, audit_op_id=op_id, audit_source="proxy_request",
                audit_path=path, audit_model=model,
            )
```

- [ ] **Step 5: Run, verify pass**

Run: `cd <repo> && uv run pytest tests/test_anthropic_standby_pick.py -q`
Expected: PASS. (Handler-level promotion is covered indirectly; the unit test locks the promote contract, and Task 2's failover test proves a standby is picked. If `tests/test_anthropic_proxy_oauth_messages.py` has a full request fixture, add an e2e asserting a served standby's role becomes primary — otherwise the unit + failover tests suffice.)

- [ ] **Step 6: Commit**
```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_standby_pick.py
git commit -m "feat(pool): auto-promote standby to primary on first serve

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Keep-warm — refresh-only; exclude standby from footprint

**Files:**
- Modify: `src/smart_proxy/anthropic_proxy.py` — `ensure_valid_token`/`_refresh_locked` (`:433`) add `activate` flag; `_run_oauth_smoke_pass` (`:3070`) role branch; `_build_oauth_usage_payload` (`:2198`) skip standby
- Test: `tests/test_anthropic_oauth_refresh.py`

**Interfaces:**
- Consumes: Task 1 `role`.
- Produces: `ensure_valid_token(..., activate: bool = True)`; smoke pass keep-warms standbys refresh-only and skips their inference; usage poller skips standbys.

- [ ] **Step 1: Write failing tests** — add to `tests/test_anthropic_oauth_refresh.py`:
```python
    def test_ensure_valid_token_activate_false_skips_activation(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    await db.insert_anthropic_key(id="k", key_type="oauth", access_token="old",
                        refresh_token="old-r", client_id="c", expires_at=0,
                        scopes='["user:inference"]', name="k")
                    pool = AnthropicKeyPool(db); await pool.reload(); key = pool.pick()
                    client = _FakeAsyncClient({"access_token": "new", "refresh_token": "new-r", "expires_in": 28800})
                    token = await pool.ensure_valid_token(key, client, activate=False)
                    self.assertEqual(token, "new")
                    self.assertEqual(len(client.request_calls), 0)     # NO activation calls
                    self.assertEqual((await db.get_anthropic_key("k"))["access_token"], "new")
                finally:
                    await db.close()
        asyncio.run(run())
```

- [ ] **Step 2: Run, verify failure**

Run: `cd <repo> && uv run pytest tests/test_anthropic_oauth_refresh.py -k activate_false -v`
Expected: FAIL — `activate` kwarg unknown / activation still runs (`request_calls` == 6).

- [ ] **Step 3: Thread the `activate` flag.**
In `ensure_valid_token` signature (`:433`) add `activate: bool = True`, and pass it into the `_refresh_locked(...)` call (both the `ensure_future(self._refresh_locked(...))` args). In `_refresh_locked` signature add `activate: bool`. Gate the success-path activation block:
```python
            if activate:
                try:
                    await activate_oauth_access_token(client, access_token=new_token, base_url=UPSTREAM_BASE)
                except Exception as exc:
                    logger.warning("OAuth activation warmup failed for key %s: %s", key.key_id[:12], exc)
                    await _record_anthropic_event(
                        self._db, key_id=key.key_id, event_type="activation_failed",
                        op_id=audit_op_id, source=audit_source, decision="note",
                        path=audit_path, model=audit_model,
                        error_type="activation_runtime_error", error_message=str(exc))
```
(Wrap the existing activation try/except in `if activate:` — do not otherwise change it.)

- [ ] **Step 4: Smoke pass — standby refresh-only, no inference.** In `_run_oauth_smoke_pass` loop (`:3070`), after `key` is resolved and `was_expired = key.is_expired()`, branch on role. Replace the single `ensure_valid_token(...)` call and following block with:
```python
        is_standby = key.role == "standby"
        effective_token = await pool.ensure_valid_token(
            key, client, audit_op_id=op_id,
            audit_source=("standby_keepwarm" if is_standby else "scheduled_smoke"),
            audit_path="/v1/messages", audit_model=_SMOKE_MODEL, activate=not is_standby,
        )
        if effective_token == pool._REFRESH_BLOCKED:
            logger.info("Smoke %s refresh blocked for key %s", window_name, key.key_id[:12])
            continue
        if effective_token is None:
            await pool.deactivate(key, audit_op_id=op_id,
                audit_source=("standby_keepwarm" if is_standby else "scheduled_smoke"),
                audit_path="/v1/messages", audit_model=_SMOKE_MODEL,
                audit_error_type="oauth_refresh_failed",
                audit_error_message="Scheduled smoke could not obtain a valid OAuth token",
                audit_context={"window_name": window_name})
            should_reload = True
            continue
        if was_expired and all(k.key_id != key.key_id for k in pool._keys):
            should_reload = True
        if is_standby:
            continue   # keep-warm only — no inference footprint for a dormant standby
```
(Leave the existing `req = _build_oauth_smoke_request(...)` / `client.send` block below unchanged for primaries.)

- [ ] **Step 5: Usage poller — skip standbys.** In `_build_oauth_usage_payload` loop (`:2198`), at the top of the `for row in oauth_rows:` body, add:
```python
        if str(row.get("role") or "primary") == "standby":
            continue   # standby: no usage GET, no refresh/activation footprint (usage == primary's)
```

- [ ] **Step 6: Add the smoke + usage tests** — add to `tests/test_anthropic_oauth_refresh.py`:
```python
    def test_usage_payload_skips_standby(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as td:
                db = await connect_test_database(sqlite_fallback_path=str(Path(td) / "t.db"))
                try:
                    await db.insert_anthropic_key(id="stby", key_type="oauth", access_token="a",
                        refresh_token="r", client_id="c", expires_at=0,
                        scopes='["user:inference"]', name="stby", role="standby")
                    pool = AnthropicKeyPool(db); await pool.reload()
                    client = _FakeAsyncClient({"access_token": "new", "refresh_token": "nr", "expires_in": 28800})
                    keys, _ = await _build_oauth_usage_payload(pool, client, db)
                    self.assertEqual(keys, [])                 # standby absent
                    self.assertEqual(len(client.calls), 0)     # no /token refresh for standby
                finally:
                    await db.close()
        asyncio.run(run())
```
(A smoke-pass standby test can reuse the `_run_oauth_smoke_pass` harness from the same file: assert `len(client.send_calls) == 0` and `source == "standby_keepwarm"` on the refresh event for a role=standby key; primary path unchanged.)

- [ ] **Step 7: Run, verify pass**

Run: `cd <repo> && uv run pytest tests/test_anthropic_oauth_refresh.py -q`
Expected: PASS (new activate/usage tests + all existing, including the primary smoke path).

- [ ] **Step 8: Commit**
```bash
git add src/smart_proxy/anthropic_proxy.py tests/test_anthropic_oauth_refresh.py
git commit -m "feat(keepwarm): refresh-only standby keep-warm; exclude standby from smoke inference + usage poller

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Dashboard role endpoint + UI

**Files:**
- Modify: `src/smart_proxy/dashboard_api.py` — add `_api_anthropic_key_role`; register route in `register_dashboard_api`
- Modify: `web/src/views/AnthropicView.svelte` — `role` field, Role column, toggle button, `setRole` action
- Build: `web/` (`npm run build`)
- Test: `tests/test_dashboard_anthropic_keys_api.py`

**Interfaces:**
- Consumes: Task 1 `set_anthropic_key_role`, `role` payload; Task 2 `_primary_alive`.
- Produces: `POST /api/anthropic/keys/role`.

- [ ] **Step 1: Write failing tests** — add a class to `tests/test_dashboard_anthropic_keys_api.py`:
```python
class AnthropicKeyRoleTests(unittest.IsolatedAsyncioTestCase):
    def _role_app(self, row):
        app, db, pool = _mgmt_app(row)
        db.set_anthropic_key_role = AsyncMock(return_value=row is not None)
        db.get_active_anthropic_keys = AsyncMock(return_value=[row] if row else [])
        return app, db, pool

    async def test_set_standby_ok(self):
        app, db, pool = self._role_app(_key_row())
        db.get_active_anthropic_keys = AsyncMock(return_value=[_key_row(), _key_row(id="other")])
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "role": "standby"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 200)
        self.assertEqual(db.set_anthropic_key_role.await_args.args, ("key-1", "standby"))
        pool.reload.assert_awaited_once()

    async def test_invalid_role_400(self):
        app, db, _ = self._role_app(_key_row())
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "role": "bogus"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()

    async def test_refuse_demote_last_primary(self):
        app, db, _ = self._role_app(_key_row())
        db.get_active_anthropic_keys = AsyncMock(return_value=[_key_row(role="primary")])
        req = make_mocked_request("POST", "/api/anthropic/keys/role", app=app, headers=AUTH)
        req.json = AsyncMock(return_value={"id": "key-1", "role": "standby"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 400)
        db.set_anthropic_key_role.assert_not_awaited()

    async def test_requires_action_auth(self):
        pool = MagicMock(); pool.check_auth.return_value = True; pool.is_proxy_key.return_value = False
        req = make_mocked_request("POST", "/api/anthropic/keys/role",
            app={"anthropic_pool": pool, "db": MagicMock()}, headers={"Authorization": "Bearer sk-ant-x"})
        req.json = AsyncMock(return_value={"id": "key-1", "role": "standby"})
        resp = await dashboard_api._api_anthropic_key_role(req)
        self.assertEqual(resp.status, 401)
```
(`_key_row` from Task 1 now includes `"role"`; add `role="primary"` default there if not already.)

- [ ] **Step 2: Run, verify failure**

Run: `cd <repo> && uv run pytest tests/test_dashboard_anthropic_keys_api.py -k Role -v`
Expected: FAIL — `_api_anthropic_key_role` undefined.

- [ ] **Step 3: Add the endpoint** in `src/smart_proxy/dashboard_api.py` (after `_api_anthropic_key_rename`):
```python
async def _api_anthropic_key_role(request: web.Request) -> web.Response:
    if not _action_authorized(request):
        return _unauthorized()
    row, body, err = await _anthropic_key_from_body(request)
    if err is not None:
        return err
    role = str(body.get("role", "")).strip()
    if role not in ("primary", "standby"):
        return web.json_response({"error": "role must be 'primary' or 'standby'"}, status=400)
    if role == "standby":
        actives = await request.app["db"].get_active_anthropic_keys()
        other_primary = any(
            r.get("id") != row["id"] and (r.get("role") or "primary") == "primary"
            and r.get("status") != "inactive"
            for r in actives
        )
        if not other_primary:
            return web.json_response(
                {"error": "cannot demote the last active primary key"}, status=400)
    await request.app["db"].set_anthropic_key_role(
        row["id"], role,
        audit_source="dashboard", audit_event_type="role_change",
        audit_decision=("set_standby" if role == "standby" else "set_primary"),
        audit_error_type="manual_action", audit_error_message=f"Set role {role} via dashboard",
    )
    await request.app["anthropic_pool"].reload()
    return web.json_response({"ok": True, "role": role})
```
Register the route in `register_dashboard_api` next to the `/api/anthropic/keys/status` route:
```python
    app.router.add_post("/api/anthropic/keys/role", _api_anthropic_key_role)
```

- [ ] **Step 4: Run endpoint tests, verify pass**

Run: `cd <repo> && uv run pytest tests/test_dashboard_anthropic_keys_api.py -q`
Expected: PASS.

- [ ] **Step 5: UI — Role column + toggle** in `web/src/views/AnthropicView.svelte`:
- Add `role: string` to the `AKey` type.
- Add a header `<th>Role</th>` after the Subscription column, and a cell `<td>{k.role}</td>`.
- Add an action button (in the actions cell, oauth keys only):
```svelte
{#if k.key_type === 'oauth'}
  <button type="button" onclick={() => setRole(k)} disabled={busy}>
    {k.role === 'standby' ? 'Make primary' : 'Make standby'}
  </button>
{/if}
```
- Add the action:
```ts
  const setRole = (k: AKey) =>
    act(() => apiPost('/api/anthropic/keys/role',
      { id: k.id, role: k.role === 'standby' ? 'primary' : 'standby' }))
```

- [ ] **Step 6: Build the SPA**

Run: `cd <repo>/web && npm run build`
Expected: build succeeds; `../src/smart_proxy/static/app/assets/*` regenerated. (Restore `git checkout -- uv.lock` if it drifted.)

- [ ] **Step 7: Full suite**

Run: `cd <repo> && uv run pytest -q`
Expected: PASS (all green).

- [ ] **Step 8: Commit**
```bash
git add src/smart_proxy/dashboard_api.py web/src/views/AnthropicView.svelte src/smart_proxy/static/app tests/test_dashboard_anthropic_keys_api.py
git commit -m "feat(dashboard): primary/standby role endpoint + UI toggle

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Notes for the implementer

- **`AnthropicKeyPool(db=None)`** is fine for pure `pick()`/`next_available_in` unit tests (they never touch the DB). Promotion/deactivate tests use a tiny fake DB (see Task 2/3 tests).
- **Deferred (not in this plan):** alerting/auto-reauth when a key bricks; the informational `available` count still counts dormant standbys (harmless).
- **Deploy:** additive `ADD COLUMN` migration — run migrations → deploy code. No writer-stop. Built SPA assets are committed in Task 5 (per repo convention the bundle is served from `static/app`; confirm whether `static/app` is git-tracked — if gitignored like the prior build, skip adding it and let deploy rebuild).
- **Sanity after all tasks:** `uv run pytest -q` green, `git log --oneline -6` shows the five task commits, and the Anthropic tab shows a Role column with a working Make standby/primary toggle.
```
