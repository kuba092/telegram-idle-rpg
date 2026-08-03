import unittest
from copy import deepcopy

import api
from progression_systems import (
    FUTURE_LIMITED_PREMIUM_CRYSTAL_SOURCES, PREMIUM_CRYSTAL_REPEATABLE_SOURCES,
    companion_milestone_multiplier, companion_upgrade_cost, skill_cooldown_multiplier,
    progression_entry, skill_effective_multiplier, skill_upgrade_cost,
    victory_progression_reward,
)


class ProgressionSystemsTest(unittest.TestCase):
    def test_costs_grow(self):
        self.assertGreater(skill_upgrade_cost(20), skill_upgrade_cost(1))
        self.assertGreater(companion_upgrade_cost(20), companion_upgrade_cost(1))

    def test_skill_milestones_are_cumulative_but_small(self):
        expected = {
            4: (1.0, 1.0), 5: (1.02, 1.0), 10: (1.02, .98),
            20: (1.05, .98), 30: (1.05, .95), 40: (1.10, .95),
            50: (1.15, .95),
        }
        for level, (effect, cooldown) in expected.items():
            with self.subTest(level=level):
                self.assertEqual(skill_effective_multiplier(level), effect)
                self.assertEqual(skill_cooldown_multiplier(level), cooldown)

    def test_companion_uses_latest_total_multiplier(self):
        expected = {4: 1.0, 5: 1.02, 10: 1.03, 20: 1.05,
                    30: 1.07, 40: 1.10, 50: 1.15}
        for level, multiplier in expected.items():
            with self.subTest(level=level):
                self.assertEqual(companion_milestone_multiplier(level), multiplier)

    def test_legacy_progression_is_preserved_bounded_and_safe(self):
        self.assertEqual(progression_entry({"level": 37})["level"], 37)
        self.assertEqual(progression_entry({"level": 999})["level"], 50)
        self.assertEqual(progression_entry({"level": "broken", "fragments": None})["level"], 1)
        self.assertFalse(progression_entry({"owned": False, "level": 20})["owned"])

    def test_combined_cooldown_reduction_obeys_global_cap(self):
        player = {
            "level": 50,
            "skills_collection_json": '{"spore_strike":{"owned":true,"level":30}}',
            "skill_slots_json": '["spore_strike",null,null]',
            "companions_collection_json": '{"mushroom_owl":{"owned":true,"level":20}}',
            "companion_slots_json": '["mushroom_owl",null,null]',
        }
        self.assertEqual(api.effective_skill_cooldown(player, "spore_strike", 8), 5.6)

    def test_dot_snapshot_does_not_change_after_skill_upgrade(self):
        player = {
            "damage": 100, "level": 50, "equipment_json": "{}",
            "companions_collection_json": "{}", "companion_slots_json": "[]",
            "boss_active": 0,
        }
        snapshot = api.dot_snapshot(player, "venom_spores", 5, .30)
        persisted_snapshot = deepcopy(snapshot)
        upgraded = api.dot_snapshot(player, "venom_spores", 10, .30)
        self.assertEqual(snapshot, persisted_snapshot)
        self.assertNotEqual(upgraded["raw_damage_per_tick"], snapshot["raw_damage_per_tick"])

    def test_legacy_collections_preserve_levels_and_do_not_activate_unowned(self):
        companions = api.normalize_companion_collection(
            '{"forest_sprite":{"level":37},"baby_slime":{"owned":false,"level":99}}'
        )
        skills = api.normalize_skill_collection(
            '{"spore_strike":{"level":31},"thorn_burst":{"owned":false,"level":12}}'
        )
        self.assertEqual(companions["forest_sprite"]["level"], 37)
        self.assertEqual(companions["baby_slime"]["level"], 50)
        self.assertFalse(companions["baby_slime"]["owned"])
        self.assertEqual(skills["spore_strike"]["level"], 31)
        self.assertFalse(skills["thorn_burst"]["owned"])
        self.assertEqual(
            api.normalize_companion_slots('["baby_slime",null,null]', companions),
            [None, None, None],
        )
        self.assertEqual(
            api.normalize_skill_slots('["thorn_burst",null,null]', skills),
            [None, None, None],
        )

    def test_reward_is_deterministic_and_exclusive_for_normal(self):
        first = victory_progression_reward("same", 10, boss=False, elite=False)
        self.assertEqual(first, victory_progression_reward("same", 10, boss=False, elite=False))
        self.assertLessEqual(first["skill_tomes_gained"] + first["companion_essence_gained"], 1)

    def test_elite_and_boss_guarantees(self):
        elite = victory_progression_reward("elite", 30, boss=False, elite=True)
        self.assertIn(elite["skill_tomes_gained"] + elite["companion_essence_gained"], (1, 2))
        boss = victory_progression_reward("boss", 1000, boss=True, elite=False)
        self.assertEqual((boss["skill_tomes_gained"], boss["companion_essence_gained"]), (8, 8))

    def test_premium_crystals_are_reserved_outside_repeatable_loops(self):
        self.assertIn("normal_battle", PREMIUM_CRYSTAL_REPEATABLE_SOURCES)
        self.assertIn("elite_battle", PREMIUM_CRYSTAL_REPEATABLE_SOURCES)
        self.assertIn("boss_battle", PREMIUM_CRYSTAL_REPEATABLE_SOURCES)
        self.assertIn("salvage", PREMIUM_CRYSTAL_REPEATABLE_SOURCES)
        self.assertIn("skill_upgrade", PREMIUM_CRYSTAL_REPEATABLE_SOURCES)
        self.assertIn("daily_quest", FUTURE_LIMITED_PREMIUM_CRYSTAL_SOURCES)
        self.assertIn("first_stage_clear", FUTURE_LIMITED_PREMIUM_CRYSTAL_SOURCES)


if __name__ == "__main__":
    unittest.main()
