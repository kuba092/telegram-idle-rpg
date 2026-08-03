import unittest

from status_effects import StatusEffectStore, status_effect


class StatusEffectStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = StatusEffectStore(max_players=2, max_effects=32, ttl_seconds=10)

    def effect(self, effect_id, identity="a", now=1, **values):
        defaults = dict(effect_type="slow", source_id=effect_id, battle_identity=identity,
                        now=now, duration_seconds=5, target="enemy", potency=.1)
        defaults.update(values)
        return status_effect(effect_id=effect_id, **defaults)

    def test_player_and_identity_isolation(self):
        self.store.apply(1, self.effect("b", "a"), 1)
        self.store.apply(2, self.effect("a", "a"), 1)
        self.assertEqual([e.effect_id for e in self.store.list(1, "a", 2)], ["b"])
        self.assertEqual([e.effect_id for e in self.store.list(2, "a", 2)], ["a"])
        self.assertEqual(self.store.list(1, "other", 2), [])

    def test_ttl_and_deterministic_order(self):
        self.store.apply(1, self.effect("z", source_id="z"), 1)
        self.store.apply(1, self.effect("a", source_id="a"), 1)
        self.assertEqual([e.effect_id for e in self.store.list(1, "a", 2)], ["a", "z"])
        self.store.list(2, "a", 12)
        self.assertEqual(self.store.list(1, "a", 12), [])

    def test_snapshot_restore(self):
        self.store.apply(1, self.effect("a"), 1)
        snapshot = self.store.snapshot(1)
        self.store.remove(1, "a")
        self.store.restore(1, snapshot)
        self.assertEqual([e.effect_id for e in self.store.list(1, "a", 2)], ["a"])

    def test_max_32_effects(self):
        for index in range(32):
            self.store.apply(1, self.effect(str(index), source_id=str(index),
                                             stack_rule="independent", max_stacks=32), 1)
        with self.assertRaises(OverflowError):
            self.store.apply(1, self.effect("overflow", source_id="overflow",
                                             stack_rule="independent", max_stacks=32), 1)

    def test_refresh_does_not_stack_potency(self):
        self.store.apply(1, self.effect("first", source_id="roots", potency=.1), 1)
        mutation = self.store.apply(1, self.effect("second", source_id="roots", now=2, potency=.2), 2)
        self.assertEqual(mutation.action, "refreshed")
        self.assertEqual(len(self.store.list(1, "a", 3)), 1)
        self.assertEqual(mutation.effect.potency, .2)

    def test_independent_limit_replaces_oldest(self):
        for index in range(3):
            self.store.apply(1, self.effect(
                str(index), now=index + 1, effect_type="damage_over_time",
                source_id="venom_spores", damage_type="poison", stack_rule="independent",
                max_stacks=2, max_ticks=4, tick_interval_seconds=1,
                next_tick_at=index + 2,
            ), index + 1)
        self.assertEqual([e.effect_id for e in self.store.list(1, "a", 3.5)], ["1", "2"])

    def test_due_ticks_claim_once_and_order(self):
        self.store.apply(1, self.effect(
            "b", effect_type="damage_over_time", damage_type="poison",
            max_ticks=3, tick_interval_seconds=1, next_tick_at=2,
        ), 1)
        ticks = self.store.due_ticks(1, "a", 4, 32)
        self.assertEqual([index for _, index, _ in ticks], [1, 2, 3])
        self.assertEqual(self.store.due_ticks(1, "a", 4, 32), [])

    def test_cleanse_priority_and_nondispellable(self):
        self.store.apply(1, self.effect("dot", effect_type="damage_over_time", target="hero"), 1)
        self.store.apply(1, self.effect("silence", effect_type="silence", target="hero"), 1)
        self.store.apply(1, self.effect("root", effect_type="root", target="hero", dispellable=False), 1)
        cleaned = self.store.cleanse_one(1, "a", "hero", 2)
        self.assertEqual(cleaned.effect_type, "silence")
        self.assertTrue(any(e.effect_id == "root" for e in self.store.list(1, "a", 2)))


if __name__ == "__main__":
    unittest.main()
