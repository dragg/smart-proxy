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

if __name__ == "__main__": unittest.main()
