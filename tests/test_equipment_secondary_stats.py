import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api
from equipment_stats import (
    ATTACKING,
    SECONDARY_STATS,
    aggregate_secondary_stats,
    build_score,
    generate_secondary_stats,
    normalize_item,
)


class EquipmentSecondaryStatsTest(unittest.TestCase):
    def generate(self, rarity, focus="mixed", seed=1):
        return generate_secondary_stats(rarity, 20, 400, focus, random.Random(seed))

    def test_rarity_counts_and_no_duplicates(self):
        self.assertEqual(self.generate("common"), {})
        self.assertEqual(len(self.generate("legendary")), 2)
        self.assertEqual(len(self.generate("celestial")), 4)
        for rarity in ("uncommon", "rare", "epic", "legendary", "mythic", "ancient", "divine", "celestial"):
            stats = self.generate(rarity)
            self.assertEqual(len(stats), len(set(stats)))

    def test_attacking_slots_prefer_attacking_stats(self):
        attack_hits = 0
        defense_hits = 0
        for seed in range(500):
            attack_hits += sum(key in ATTACKING for key in self.generate("celestial", "attack", seed))
            defense_hits += sum(key in ATTACKING for key in self.generate("celestial", "defense", seed))
        self.assertGreater(attack_hits, defense_hits)

    def test_old_and_unknown_stats_are_safe(self):
        self.assertEqual(normalize_item({"power": 3})["secondary_stats"], {})
        self.assertEqual(normalize_item({"secondary_stats": {"future_stat": 99}})["secondary_stats"], {})

    def test_aggregate_caps(self):
        equipment = {str(i): {"secondary_stats": {key: 999 for key in SECONDARY_STATS}} for i in range(5)}
        totals = aggregate_secondary_stats(equipment)
        self.assertLessEqual(totals["attack_speed"], 50)
        self.assertLessEqual(totals["crit_chance"], 100)
        self.assertLessEqual(totals["incoming_damage_reduction"], 60)
        for key in ("dodge_chance", "combo_chance", "counter_chance"):
            self.assertLessEqual(totals[key], 50)

    def test_combat_multipliers_and_attack_interval(self):
        equipment = {"weapon": {"secondary_stats": {
            "attack_speed": 50, "skill_damage": 100, "companion_damage": 100,
            "boss_damage": 50, "incoming_damage_reduction": 60,
            "healing_bonus": 100,
        }}}
        total = api.calculate_equipment_stats(equipment, 1)["total"]
        self.assertEqual(total["attack_speed_multiplier"], 1.5)
        self.assertEqual(total["skill_damage_multiplier"], 2.0)
        self.assertEqual(total["companion_damage_multiplier"], 2.0)
        self.assertEqual(total["boss_damage_multiplier"], 1.5)
        self.assertEqual(total["incoming_damage_multiplier"], .4)
        self.assertEqual(total["healing_multiplier"], 2.0)
        self.assertLess(1 / total["attack_speed_multiplier"], 1.0)

    def test_build_profiles_choose_their_archetype(self):
        attack = {"power": 90, "damage": 10, "hp": 0, "secondary_stats": {"attack_speed": 20, "crit_chance": 15}}
        defense = {"power": 100, "damage": 0, "hp": 100, "secondary_stats": {"incoming_damage_reduction": 20, "dodge_chance": 10}}
        self.assertGreater(build_score(attack, "damage"), build_score(defense, "damage"))
        self.assertGreater(build_score(defense, "defense"), build_score(attack, "defense"))
        damage_gap = build_score(attack, "damage") - build_score(defense, "damage")
        balanced_gap = build_score(attack, "balanced") - build_score(defense, "balanced")
        self.assertLess(abs(balanced_gap), abs(damage_gap))
        self.assertNotEqual(attack["power"], build_score(attack, "damage"))

    def test_seeded_generation_is_deterministic(self):
        state = random.getstate()
        try:
            random.seed(1234)
            first = api.generate_loot(300, 15)
            random.seed(1234)
            second = api.generate_loot(300, 15)
        finally:
            random.setstate(state)
        self.assertEqual(first, second)

    def test_comparison_uses_build_score_for_auto_sell_decision(self):
        equipped = {"power": 100, "damage": 0, "hp": 0, "secondary_stats": {}}
        candidate = {"slot": "weapon", "power": 90, "damage": 10, "hp": 0,
                     "secondary_stats": {"attack_speed": 20, "crit_chance": 15}}
        player = {"level": 1, "comparison_profile": "damage",
                  "equipment_json": __import__("json").dumps({"weapon": equipped})}
        comparison = api.compare_loot(player, candidate)
        self.assertLess(comparison["raw_power"], comparison["equipped_raw_power"])
        self.assertTrue(comparison["is_improvement"])

    def test_comparison_profile_migrates_and_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "game.db")
            with patch.object(api, "DATABASE_PATH", database):
                api.create_database()
                connection = sqlite3.connect(database)
                connection.execute("INSERT INTO players (telegram_id, updated_at) VALUES (1, 0)")
                connection.commit()
                connection.close()
                with patch.object(api, "validate_telegram_data", return_value={"id": 1}), patch.object(
                    api, "get_or_create_player", return_value={"telegram_id": 1}
                ):
                    response = api.set_comparison_profile("damage", "test")
                self.assertEqual(response["comparison_profile"], "damage")
                self.assertTrue(response["comparison_profile_saved"])


if __name__ == "__main__":
    unittest.main()
