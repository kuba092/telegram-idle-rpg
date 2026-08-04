import json
import random
import sqlite3
import tempfile
import unittest

import api
from equipment_stats import SECONDARY_STATS, build_score, normalize_item, reroll_secondary_stat
from loot_progression import (
    chest_progress, chest_upgrade_gold_cost, hero_exp_from_open,
    hero_exp_from_sale, inventory_capacity, rarity_weights, reroll_cost,
    salvage_reward,
)


class LootProgressionFormulaTest(unittest.TestCase):
    def test_old_item_defaults_are_read_only_safe(self):
        item = normalize_item({"id": "old", "rarity": "rare", "secondary_stats": {"crit_chance": 2}})
        self.assertFalse(item["locked"])
        self.assertEqual(item["reroll_count"], 0)
        self.assertEqual(item["item_version"], 1)
        self.assertEqual(item["reroll_history"], [])

    def test_salvage_scales_by_rarity_and_is_deterministic(self):
        common = salvage_reward({"id": "a", "rarity": "common"}, 5, 100)
        legendary = salvage_reward({"id": "b", "rarity": "legendary"}, 5, 100)
        self.assertGreater(legendary["dust"], common["dust"])
        self.assertEqual(common, salvage_reward({"id": "a", "rarity": "common"}, 5, 100))

    def test_crystal_ranges(self):
        self.assertEqual(salvage_reward({"id": "x", "rarity": "rare"}, 1, 1)["crystals"], 0)
        self.assertEqual(salvage_reward({"id": "x", "rarity": "mythic"}, 1, 1)["crystals"], 1)
        self.assertIn(salvage_reward({"id": "x", "rarity": "celestial"}, 1, 1)["crystals"], range(3, 6))

    def test_chest_progress_and_capacity(self):
        required = chest_upgrade_gold_cost(1)
        progress = chest_progress({"chest_level": 1, "gold": required, "chest_xp": 999})
        self.assertTrue(progress["chest_upgrade_ready"])
        self.assertEqual(progress["chest_xp"], 0)
        self.assertEqual(hero_exp_from_open(1, 1), round(3 + .8 + .04))
        self.assertEqual(hero_exp_from_sale("celestial", 1000), round(55 * 3))
        self.assertEqual(inventory_capacity(1), 50)
        self.assertEqual(inventory_capacity(5), 55)
        self.assertEqual(inventory_capacity(999), 100)

    def test_rarity_moves_up_but_celestial_stays_rare(self):
        low, high = rarity_weights(1), rarity_weights(30)
        self.assertGreater(low[0] + low[1], .9 * sum(low))
        self.assertGreater(sum(i * weight for i, weight in enumerate(high)) / sum(high),
                           sum(i * weight for i, weight in enumerate(low)) / sum(low))
        self.assertLess(high[-1] / sum(high), .001)

    def test_seeded_generation(self):
        self.assertEqual(api.generate_loot(100, 20, random.Random(42)),
                         api.generate_loot(100, 20, random.Random(42)))

    def test_reroll_changes_one_stat_and_preserves_raw_power(self):
        item = {"id": "r", "rarity": "rare", "power": 123, "slot_focus": "attack",
                "secondary_stats": {"crit_chance": 2}, "item_version": 1}
        changed = reroll_secondary_stat(item, "crit_chance", 10, 100, random.Random(7))
        self.assertEqual(changed["power"], 123)
        self.assertEqual(len(changed["secondary_stats"]), 1)
        self.assertNotIn("crit_chance", changed["secondary_stats"])
        self.assertEqual(changed["item_version"], 2)
        self.assertEqual(changed["reroll_count"], 1)
        key, value = next(iter(changed["secondary_stats"].items()))
        self.assertLessEqual(value, SECONDARY_STATS[key]["max"])
        self.assertIsInstance(build_score(changed), float)

    def test_reroll_cost_grows(self):
        self.assertGreater(reroll_cost("rare", 2)["dust"], reroll_cost("rare", 1)["dust"])


class LootMigrationTest(unittest.TestCase):
    def test_migration_is_idempotent_and_defaults_old_player(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            old_path = api.DATABASE_PATH
            try:
                api.DATABASE_PATH = database.name
                connection = sqlite3.connect(database.name)
                connection.execute("CREATE TABLE players (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, level INTEGER NOT NULL DEFAULT 1, gold INTEGER NOT NULL DEFAULT 0, enemy_hp INTEGER NOT NULL DEFAULT 30, updated_at INTEGER NOT NULL)")
                connection.execute("INSERT INTO players VALUES (1,'old','Old',1,0,30,0)")
                connection.commit(); connection.close()
                api.create_database(); api.create_database()
                connection = sqlite3.connect(database.name); connection.row_factory = sqlite3.Row
                player = dict(connection.execute("SELECT * FROM players WHERE telegram_id=1").fetchone())
                connection.close()
                self.assertEqual(player["salvage_dust"], 0)
                self.assertEqual(player["refinement_crystal"], 0)
                self.assertEqual(player["refinement_ore"], 0)
                self.assertEqual(player["chest_xp"], 0)
                self.assertEqual(json.loads(player["inventory_json"]), [])
                self.assertGreaterEqual(player["chest_level"], 1)
            finally:
                api.DATABASE_PATH = old_path

    def test_legacy_crystal_moves_to_ore_exactly_once(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as database:
            old_path = api.DATABASE_PATH
            try:
                api.DATABASE_PATH = database.name
                connection = sqlite3.connect(database.name)
                connection.execute(
                    "CREATE TABLE players (telegram_id INTEGER PRIMARY KEY, username TEXT, "
                    "first_name TEXT, level INTEGER NOT NULL DEFAULT 1, gold INTEGER NOT NULL DEFAULT 0, "
                    "enemy_hp INTEGER NOT NULL DEFAULT 30, refinement_crystal INTEGER NOT NULL DEFAULT 0, "
                    "updated_at INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO players VALUES (1,'old','Old',1,0,30,7,0)")
                connection.commit()
                connection.close()
                api.create_database()
                connection = sqlite3.connect(database.name)
                connection.execute("UPDATE players SET refinement_ore=11 WHERE telegram_id=1")
                connection.commit()
                connection.close()
                api.create_database()
                connection = sqlite3.connect(database.name)
                ore, legacy = connection.execute(
                    "SELECT refinement_ore, refinement_crystal FROM players WHERE telegram_id=1"
                ).fetchone()
                connection.close()
                self.assertEqual((ore, legacy), (11, 7))
            finally:
                api.DATABASE_PATH = old_path


if __name__ == "__main__":
    unittest.main()
