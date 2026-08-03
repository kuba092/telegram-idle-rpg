import random
import unittest
from unittest.mock import patch

import api
from combat_effects import (
    COMPANION_EFFECTS,
    CombatContext,
    CombatEffectEngine,
    CombatEvent,
    CombatStats,
    EffectDefinition,
    RefreshMode,
    TemporaryEffect,
    active_companion_sources,
)


class CombatEffectEngineTest(unittest.TestCase):
    def context(self):
        return CombatContext(current_hp=80, max_hp=100, enemy_hp=200)

    def test_event_order_is_deterministic(self):
        engine = CombatEffectEngine()
        seen = []
        engine.register_handler(CombatEvent.BATTLE_START, "zeta", lambda *_: seen.append("zeta"), 20)
        engine.register_handler(CombatEvent.BATTLE_START, "beta", lambda *_: seen.append("beta"), 10)
        engine.register_handler(CombatEvent.BATTLE_START, "alpha", lambda *_: seen.append("alpha"), 10)

        order = engine.dispatch(CombatEvent.BATTLE_START, self.context())

        self.assertEqual(order, ["alpha", "beta", "zeta"])
        self.assertEqual(seen, order)

    def test_temporary_effect_expires(self):
        context = self.context()
        effect = TemporaryEffect("poison_cloud", 5, 5, source_type="skill", source_id="poison_cloud")
        self.assertTrue(context.add_temporary_effect(effect))
        context.advance(4.9)
        self.assertEqual(len(context.temporary_effects), 1)
        context.advance(.1)
        self.assertEqual(context.temporary_effects, [])

    def test_stack_cap_and_refresh(self):
        context = self.context()
        for _ in range(5):
            context.add_temporary_effect(TemporaryEffect(
                "poison_cloud", 5, 5, max_stacks=3,
                refresh_mode=RefreshMode.REFRESH.value,
                source_type="skill", source_id="poison_cloud",
            ))
        self.assertEqual(context.temporary_effects[0].stack_count, 3)

    def test_closed_slots_do_not_create_sources(self):
        collection = {
            "forest_sprite": {"owned": True, "level": 20},
            "baby_slime": {"owned": True, "level": 20},
        }
        sources = active_companion_sources(
            ["forest_sprite", "baby_slime", None], collection, 1
        )
        self.assertEqual([item[0].effect_id for item in sources], ["forest_sprite"])

    def test_two_multiplier_sources_multiply(self):
        first = EffectDefinition("first", "test", modifiers_per_level={"damage_multiplier": .10})
        second = EffectDefinition("second", "test", modifiers_per_level={"damage_multiplier": .20})
        stats, _ = CombatEffectEngine.combine([(second, 1), (first, 1)])
        self.assertAlmostEqual(stats.damage_multiplier, 1.32)

    def test_crit_chance_is_capped(self):
        stats = CombatStats()
        stats.apply({"crit_chance_bonus": 120})
        self.assertEqual(stats.crit_chance_bonus, 100)

    def test_incoming_damage_cannot_be_negative(self):
        stats = CombatStats()
        stats.apply({"incoming_damage_multiplier": -2})
        self.assertEqual(stats.incoming_damage_multiplier, 0)

    def test_unknown_temporary_effect_is_rejected(self):
        context = self.context()
        accepted = context.add_temporary_effect(TemporaryEffect("not_registered", 1, 1))
        self.assertFalse(accepted)
        self.assertEqual(context.temporary_effects, [])

    def test_public_serialization_is_stable(self):
        effect = TemporaryEffect(
            "poison_cloud", 5, 4, 2, 3, "refresh", "skill", "poison_cloud",
            {"skill_damage_multiplier": 1.1},
        )
        self.assertEqual(list(effect.public()), [
            "effect_id", "duration_seconds", "remaining_duration", "stack_count",
            "max_stacks", "refresh_mode", "source_type", "source_id", "modifiers",
        ])
        self.assertEqual(effect.public(), effect.public())

    def test_same_seed_produces_same_combat_result(self):
        player = {
            "damage": 100, "level": 1, "equipment_json": "{}",
            "companions_collection_json": "{}", "companion_slots_json": "[]",
        }
        with patch.object(api, "calculate_equipment_stats", return_value={
            "total": {"crit_chance": 50.0, "crit_damage": 150.0}
        }):
            random.seed(8128)
            first = api.calculate_hero_attack_damage(player)
            random.seed(8128)
            second = api.calculate_hero_attack_damage(player)
        self.assertEqual(first, second)

    def test_published_companion_values_are_registered(self):
        self.assertEqual(set(COMPANION_EFFECTS), {
            "forest_sprite", "baby_slime", "spore_beetle", "mushroom_owl",
            "thorn_wolf", "ancient_entling",
        })


if __name__ == "__main__":
    unittest.main()
