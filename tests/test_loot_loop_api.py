import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import api
from equipment_stats import build_score
from loot_progression import chest_upgrade_gold_cost, hero_exp_from_open, hero_exp_from_sale, inventory_capacity


class LootLoopApiIntegrationTest(unittest.TestCase):
    """Exercise the real route functions against a migrated SQLite database."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_directory.name) / "game.db")
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.auth_patch = patch.object(api, "validate_telegram_data", return_value={"id": 7001})
        self.auth_patch.start()
        api.create_database()
        connection = api.get_database()
        connection.execute(
            "INSERT INTO players (telegram_id, username, first_name, updated_at) VALUES (7001,'loot','Loot',0)"
        )
        connection.commit()
        connection.close()
        self.player_patch = patch.object(api, "get_or_create_player", side_effect=self.load_player)
        self.player_patch.start()
        api.ACTION_CACHE.clear()

    def tearDown(self):
        api.ACTION_CACHE.clear()
        self.player_patch.stop()
        self.auth_patch.stop()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def load_player(self, *_args, **_kwargs):
        connection = api.get_database()
        try:
            return api.load_player(connection, 7001)
        finally:
            connection.close()

    def item(self, item_id, rarity="rare", slot="weapon", power=10,
             locked=False, version=1, stats=None):
        return {
            "id": str(item_id), "item_id": str(item_id), "slot": slot,
            "slot_name": slot, "slot_focus": "attack", "icon": "⚔️",
            "name": f"Item {item_id}", "rarity": rarity, "rarity_name": rarity,
            "power": power, "damage": max(1, power // 2), "hp": 0,
            "secondary_stats": stats if stats is not None else {"crit_chance": 2.0},
            "sell_price": 5, "stage_found": 20, "chest_level_found": 1,
            "locked": locked, "reroll_count": 0, "item_version": version,
            "reroll_history": [],
        }

    def update_player(self, **values):
        connection = api.get_database()
        try:
            assignments = ",".join(f"{key}=?" for key in values)
            connection.execute(
                f"UPDATE players SET {assignments} WHERE telegram_id=?",
                (*values.values(), 7001),
            )
            connection.commit()
        finally:
            connection.close()

    def snapshot(self):
        player = self.load_player()
        return {
            "inventory": api.normalized_inventory(player),
            "equipment": api.parse_json_object(player["equipment_json"]),
            "dust": player["salvage_dust"], "ore": player["refinement_ore"],
            "premium_crystals": player["premium_crystals"],
            "chest_xp": player["chest_xp"], "chests": player["chests"],
            "chest_level": player["chest_level"], "pending": player["pending_loot_json"],
        }

    def call_salvage(self, item, action="salvage-1", version=None):
        return api.salvage_inventory({
            "item_id": item["id"],
            "expected_item_version": item["item_version"] if version is None else version,
            "client_action_id": action,
        }, "test")

    def open_generated(self, item, auto_mode=False):
        connection = api.get_database()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = api.load_player(connection, 7001)
            with patch.object(api, "generate_loot", return_value=item):
                updated, result = api.open_loot_transaction(connection, current, auto_mode)
            connection.commit()
            return updated, result
        finally:
            connection.close()

    def test_open_grants_hero_exp_once_and_never_chest_xp(self):
        item = self.item("open-exp")
        self.update_player(chests=2, chest_level=4, highest_stage=50, chest_xp=777, experience=10)
        _, first = self.open_generated(item)
        after_first = self.load_player()
        _, repeated = self.open_generated(item)
        after_repeat = self.load_player()
        expected = hero_exp_from_open(4, 50)
        self.assertEqual(first["hero_exp_gained"], expected)
        self.assertEqual(first["hero_exp_before"], 10)
        self.assertEqual(first["hero_exp_after"], 10 + expected)
        self.assertTrue(repeated["pending_exists"])
        self.assertEqual(after_repeat["experience"], after_first["experience"])
        self.assertEqual(after_repeat["chest_xp"], 777)

    def test_open_route_duplicate_action_generates_exactly_one_item(self):
        item = self.item("route-once")
        self.update_player(chests=2, inventory_json=json.dumps(self.full_inventory()), experience=0)
        with patch.object(api, "generate_loot", return_value=item):
            first = api.open_loot({"client_action_id": "open-once"}, "test")
            duplicate = api.open_loot({"client_action_id": "open-once"}, "test")
        state = self.load_player()
        self.assertTrue(first["transaction_completed"])
        self.assertTrue(duplicate["duplicate_request"])
        self.assertEqual(state["chests"], 1)
        self.assertEqual(len(api.normalized_inventory(state)), inventory_capacity(1))
        self.assertEqual(api.public_pending_loot(state)["item_id"], "route-once")
        self.assertEqual(state["experience"], first["hero_exp_after"])

    def test_pending_sale_grants_gold_and_sale_exp_only_once(self):
        item = self.item("sale-exp", rarity="legendary")
        self.update_player(chests=1, highest_stage=100, gold=9, experience=0)
        self.open_generated(item)
        after_open = self.load_player()
        sold = api.sell_loot({"client_action_id": "sell-once"}, "test")
        after_sale = self.load_player()
        self.assertEqual(sold["gold_gained"], item["sell_price"])
        self.assertEqual(sold["hero_exp_gained"], hero_exp_from_sale("legendary", 100))
        self.assertEqual(after_sale["experience"] - after_open["experience"], sold["hero_exp_gained"])
        self.assertTrue(sold["pending_cleared"])
        duplicate = api.sell_loot({"client_action_id": "sell-once"}, "test")
        self.assertTrue(duplicate["duplicate_request"])

    def test_equip_sells_replaced_item_without_regranting_open_exp(self):
        replaced = self.item("old", rarity="epic")
        replaced["sell_price"] = 11
        pending = self.item("new", slot="weapon", power=50)
        self.update_player(equipment_json=json.dumps({"weapon": replaced}), inventory_json=json.dumps([pending]),
                           pending_loot_json=json.dumps(pending), highest_stage=200, gold=3, experience=20)
        equipped = api.equip_loot({"client_action_id": "equip-once"}, "test")
        state = self.load_player()
        expected_exp = hero_exp_from_sale("epic", 200)
        self.assertEqual(equipped["gold_gained_from_replaced"], 11)
        self.assertEqual(equipped["hero_exp_gained_from_replaced"], expected_exp)
        self.assertEqual(state["experience"], 20 + expected_exp)
        self.assertEqual(state["gold"], 14)
        self.assertEqual(len(api.normalized_inventory(state)), 1)
        self.assertTrue(equipped["pending_cleared"])

    def test_auto_sale_combines_open_and_sale_exp(self):
        strong = self.item("strong-auto", slot="helmet", power=1000)
        weak = self.item("weak-auto", rarity="rare", slot="helmet", power=1)
        self.update_player(chests=1, equipment_json=json.dumps({"helmet": strong}), highest_stage=300,
                           experience=0, gold=0, chest_xp=55)
        _, result = self.open_generated(weak, auto_mode=True)
        self.assertTrue(result["auto_sold"])
        self.assertEqual(result["hero_exp_gained_from_open"], hero_exp_from_open(1, 300))
        self.assertEqual(result["hero_exp_gained_from_sale"], hero_exp_from_sale("rare", 300))
        self.assertEqual(result["hero_exp_gained"], result["hero_exp_gained_from_open"] + result["hero_exp_gained_from_sale"])
        self.assertEqual(self.load_player()["chest_xp"], 55)

    def test_hero_exp_result_supports_multiple_levels(self):
        result = api.hero_exp_result({"experience": 0, "level": 1}, api.LEVEL_TOTAL_EXP[10])
        self.assertEqual(result["hero_level_after"], 10)
        self.assertEqual(result["hero_levels_gained"], 9)

    # Salvage
    def test_salvage_success_and_duplicate_action_reward_once(self):
        item = self.item("s1", rarity="mythic")
        self.update_player(inventory_json=json.dumps([item]))
        first = self.call_salvage(item)
        second = self.call_salvage(item)
        state = self.snapshot()
        self.assertTrue(first["transaction_completed"])
        self.assertTrue(second["duplicate_request"])
        self.assertEqual(state["dust"], first["dust_gained"])
        self.assertEqual(state["ore"], first["ore_gained"])
        self.assertEqual(state["premium_crystals"], 0)
        self.assertEqual(state["chest_xp"], first["chest_xp_gained"])
        self.assertEqual(state["inventory"], [])
        for key in ("salvaged_item_id", "rarity", "dust_gained", "crystals_gained",
                    "inventory_count", "stale_item", "transaction_completed"):
            self.assertIn(key, first)

    def test_salvage_rejects_equipped_and_locked(self):
        equipped = self.item("eq")
        locked = self.item("locked", locked=True)
        self.update_player(equipment_json=json.dumps({"weapon": equipped}),
                           inventory_json=json.dumps([locked]))
        with self.assertRaises(HTTPException):
            self.call_salvage(equipped, "equipped")
        with self.assertRaises(HTTPException):
            self.call_salvage(locked, "locked")
        self.assertEqual(len(self.snapshot()["inventory"]), 1)

    def test_salvage_stale_version_changes_nothing(self):
        item = self.item("stale", version=4)
        self.update_player(inventory_json=json.dumps([item]), salvage_dust=9,
                           refinement_ore=2, premium_crystals=11, chest_xp=7)
        before = self.snapshot()
        result = self.call_salvage(item, "stale-action", version=3)
        self.assertTrue(result["stale_item"])
        self.assertFalse(result["transaction_completed"])
        self.assertEqual(self.snapshot(), before)

    def test_salvage_rollback_restores_item_and_resources(self):
        item = self.item("rollback")
        self.update_player(inventory_json=json.dumps([item]), salvage_dust=9,
                           refinement_ore=2, premium_crystals=11, chest_xp=7)
        before = self.snapshot()
        with patch.object(api, "salvage_reward", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.call_salvage(item, "rollback-action")
        self.assertEqual(self.snapshot(), before)

    # Bulk salvage
    def test_bulk_duplicate_missing_and_totals(self):
        first, second = self.item("b1"), self.item("b2", rarity="epic")
        self.update_player(inventory_json=json.dumps([first, second]))
        result = api.salvage_inventory_bulk({
            "item_ids": ["b1", "b1", "missing", "b2"],
            "exclude_build_upgrades": False,
        }, "test")
        self.assertEqual(result["requested_count"], 3)
        self.assertEqual(result["salvaged_count"], 2)
        self.assertEqual(result["missing_items"], ["missing"])
        self.assertEqual(result["total_dust"], sum(row["dust"] for row in result["results"]))
        self.assertEqual(result["total_crystals"], sum(row["crystals"] for row in result["results"]))
        state = self.snapshot()
        self.assertEqual(state["dust"], result["total_dust"])
        self.assertEqual(state["ore"], result["total_crystals"])
        self.assertEqual(state["premium_crystals"], 0)

    def test_bulk_skips_equipped_locked_and_build_upgrade(self):
        equipped = self.item("equipped", power=100)
        locked = self.item("locked", locked=True, slot="helmet")
        upgrade = self.item("upgrade", power=200)
        self.update_player(equipment_json=json.dumps({"weapon": equipped}),
                           inventory_json=json.dumps([equipped, locked, upgrade]))
        result = api.salvage_inventory_bulk({
            "item_ids": ["equipped", "locked", "upgrade"],
            "exclude_build_upgrades": True,
        }, "test")
        self.assertEqual(result["skipped_equipped"], ["equipped"])
        self.assertEqual(result["skipped_locked"], ["locked"])
        self.assertEqual(result["skipped_build_upgrade"], ["upgrade"])
        self.assertEqual(result["salvaged_count"], 0)

    def test_bulk_hard_limit_is_100(self):
        items = [self.item(f"limit-{index}", power=1) for index in range(105)]
        self.update_player(inventory_json=json.dumps(items))
        result = api.salvage_inventory_bulk({
            "item_ids": [item["id"] for item in items],
            "exclude_build_upgrades": False,
        }, "test")
        self.assertEqual(result["requested_count"], 100)
        self.assertEqual(result["salvaged_count"], 100)
        self.assertEqual(len(self.snapshot()["inventory"]), 5)

    # Reroll
    def test_reroll_updates_one_stat_score_version_count_and_history(self):
        item = self.item("reroll", rarity="rare", power=123)
        self.update_player(inventory_json=json.dumps([item]), salvage_dust=1000,
                           refinement_ore=100, premium_crystals=17)
        old_score = build_score(item, "balanced")
        result = api.reroll_inventory_secondary({
            "item_id": "reroll", "stat_key": "crit_chance",
            "expected_item_version": 1, "client_action_id": "reroll-1",
        }, "test")
        changed = self.snapshot()["inventory"][0]
        self.assertEqual(changed["power"], 123)
        self.assertEqual(len(changed["secondary_stats"]), 1)
        self.assertNotIn("crit_chance", changed["secondary_stats"])
        self.assertEqual(result["item_version"], 2)
        self.assertEqual(result["reroll_count"], 1)
        self.assertEqual(result["build_score"], build_score(changed, "balanced"))
        self.assertNotEqual(result["build_score"], old_score)
        self.assertEqual(len(changed["reroll_history"]), 1)
        self.assertEqual(result["raw_power"], 123)

    def test_reroll_history_is_limited_to_five(self):
        item = self.item("history", rarity="rare")
        self.update_player(inventory_json=json.dumps([item]), salvage_dust=100000)
        version, stat = 1, "crit_chance"
        for index in range(7):
            result = api.reroll_inventory_secondary({
                "item_id": "history", "stat_key": stat,
                "expected_item_version": version, "client_action_id": f"history-{index}",
            }, "test")
            version, stat = result["item_version"], result["new_stat"]
        changed = self.snapshot()["inventory"][0]
        self.assertEqual(changed["reroll_count"], 7)
        self.assertEqual(len(changed["reroll_history"]), 5)

    def test_reroll_stale_and_insufficient_resources_change_nothing(self):
        item = self.item("no-change", rarity="legendary", version=3)
        self.update_player(inventory_json=json.dumps([item]), salvage_dust=0,
                           refinement_ore=0, premium_crystals=19)
        before = self.snapshot()
        stale = api.reroll_inventory_secondary({
            "item_id": "no-change", "stat_key": "crit_chance",
            "expected_item_version": 2, "client_action_id": "stale-reroll",
        }, "test")
        self.assertTrue(stale["stale_item"])
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(HTTPException):
            api.reroll_inventory_secondary({
                "item_id": "no-change", "stat_key": "crit_chance",
                "expected_item_version": 3, "client_action_id": "poor-reroll",
            }, "test")
        self.assertEqual(self.snapshot(), before)

    def test_reroll_rollback_restores_equipped_item_and_resources(self):
        item = self.item("equipped-reroll", rarity="legendary")
        self.update_player(equipment_json=json.dumps({"weapon": item}), salvage_dust=100,
                           refinement_ore=10, premium_crystals=23)
        before = self.snapshot()
        with patch.object(api, "sync_player_stats", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                api.reroll_inventory_secondary({
                    "item_id": item["id"], "stat_key": "crit_chance",
                    "expected_item_version": 1, "client_action_id": "rollback-reroll",
                }, "test")
        self.assertEqual(self.snapshot(), before)

    # Gold chest upgrade
    def test_chest_upgrade_requires_gold_preserves_remainder_and_is_idempotent(self):
        required = chest_upgrade_gold_cost(1)
        self.update_player(chest_level=1, gold=required - 1, chest_xp=999999, premium_crystals=17)
        rejected = api.upgrade_chest({"client_action_id": "not-ready", "expected_level": 1}, "test")
        self.assertFalse(rejected["transaction_completed"])
        self.assertEqual(self.snapshot()["chest_level"], 1)
        self.update_player(gold=required + 7)
        first = api.upgrade_chest({"client_action_id": "upgrade-once", "expected_level": 1}, "test")
        second = api.upgrade_chest({"client_action_id": "upgrade-once", "expected_level": 1}, "test")
        self.assertTrue(first["transaction_completed"])
        self.assertEqual(first["gold_remaining"], 7)
        self.assertTrue(second["duplicate_request"])
        self.assertEqual(self.snapshot()["chest_level"], 2)
        self.assertEqual(self.snapshot()["premium_crystals"], 17)
        stale = api.upgrade_chest({"client_action_id": "stale-level", "expected_level": 1}, "test")
        self.assertTrue(stale["stale_level"])
        self.assertEqual(self.load_player()["gold"], 7)

    def test_chest_upgrade_cap_and_migration_rerun(self):
        self.update_player(chest_level=30, chest_xp=999999)
        result = api.upgrade_chest({"client_action_id": "at-cap"}, "test")
        self.assertTrue(result["cap_reached"])
        self.assertFalse(result["transaction_completed"])
        self.assertEqual(self.snapshot()["chest_level"], 30)
        api.create_database()
        api.create_database()
        self.assertEqual(self.snapshot()["chest_level"], 30)

    # Inventory capacity and auto salvage
    def full_inventory(self):
        return [self.item(f"full-{index}", slot="helmet", power=1) for index in range(inventory_capacity(1))]

    def test_inventory_full_does_not_block_or_receive_new_pending_item(self):
        self.update_player(inventory_json=json.dumps(self.full_inventory()), chests=3,
                           pending_loot_json="")
        generated = self.item("generated", slot="helmet")
        connection = api.get_database()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = api.load_player(connection, 7001)
            with patch.object(api, "generate_loot", return_value=generated):
                updated, result = api.open_loot_transaction(connection, current, False)
            connection.commit()
        finally:
            connection.close()
        self.assertTrue(result["item_generated"])
        state = self.snapshot()
        self.assertEqual(state["chests"], 2)
        self.assertEqual(len(state["inventory"]), 50)
        self.assertEqual(json.loads(state["pending"])["item_id"], "generated")
        self.assertEqual(updated["chests"], 2)

    def test_legacy_auto_salvage_setting_does_not_bypass_pending_decision(self):
        equipped = self.item("strong", power=1000)
        base_values = dict(inventory_json=json.dumps(self.full_inventory()), chests=3,
                           equipment_json=json.dumps({"helmet": equipped}),
                           auto_salvage_enabled=1, auto_salvage_max_rarity="rare")
        self.update_player(**base_values)

        _, pending = self.open_generated(self.item("auto", slot="helmet", power=1), auto_mode=False)
        self.assertTrue(pending["item_generated"])
        self.assertFalse(pending["auto_salvaged"])
        self.assertEqual(len(self.snapshot()["inventory"]), 50)
        self.assertEqual(self.snapshot()["chests"], 2)

    # Compatibility and public contract
    def test_legacy_item_unknown_values_and_player_public_contract(self):
        legacy = {"id": "legacy", "slot": "weapon", "name": "Legacy",
                  "rarity": "future", "power": 5, "damage": 1, "hp": 0,
                  "secondary_stats": {"future_stat": 999}, "sell_price": 2}
        self.update_player(inventory_json=json.dumps([legacy]))
        response = api.build_player_response(self.load_player())
        self.assertIn("loot_progression", response)
        public = response["inventory"][0]
        for key in ("item_id", "item_version", "locked", "reroll_count",
                    "reroll_history_summary", "secondary_stats", "raw_power",
                    "build_score", "comparison", "salvage_value", "reroll_cost",
                    "can_reroll", "can_salvage", "equipped"):
            self.assertIn(key, public)
        self.assertEqual(public["secondary_stats"], {})
        self.assertEqual(public["salvage_value"]["rarity"], "common")

    def test_legacy_equip_sell_and_open_routes_keep_response_fields(self):
        equip_item = self.item("legacy-equip")
        self.update_player(inventory_json=json.dumps([equip_item]),
                           pending_loot_json=json.dumps(equip_item))
        equipped = api.equip_loot({}, "test")
        for key in ("equipped", "item", "comparison", "replaced_item", "replaced_reward", "message"):
            self.assertIn(key, equipped)

        sell_item = self.item("legacy-sell", slot="helmet")
        self.update_player(inventory_json=json.dumps([sell_item]),
                           pending_loot_json=json.dumps(sell_item))
        sold = api.sell_loot({}, "test")
        for key in ("sold", "item", "sell_price", "message"):
            self.assertIn(key, sold)

        generated = self.item("legacy-open", slot="helmet")
        self.update_player(inventory_json="[]", pending_loot_json="", chests=1)
        with patch.object(api, "generate_loot", return_value=generated):
            opened = api.open_loot({}, "test")
        for key in ("opened", "loot", "comparison", "experience_reward", "message"):
            self.assertIn(key, opened)

    def test_new_routes_have_stable_response_fields(self):
        item = self.item("stable")
        self.update_player(inventory_json=json.dumps([item]), salvage_dust=100)
        locked = api.lock_inventory_item({"item_id": "stable", "locked": True,
                                          "expected_item_version": 1,
                                          "client_action_id": "stable-lock"}, "test")
        for key in ("item_id", "locked", "item_version", "stale_item",
                    "transaction_completed", "duplicate_request"):
            self.assertIn(key, locked)
        unlocked = api.lock_inventory_item({"item_id": "stable", "locked": False,
                                            "expected_item_version": 2,
                                            "client_action_id": "stable-unlock"}, "test")
        rerolled = api.reroll_inventory_secondary({"item_id": "stable", "stat_key": "crit_chance",
                                                   "expected_item_version": unlocked["item_version"],
                                                   "client_action_id": "stable-reroll"}, "test")
        for key in ("item_id", "old_stat", "new_stat", "old_value", "new_value",
                    "dust_spent", "crystals_spent", "reroll_count", "item_version",
                    "raw_power", "build_score", "comparison", "transaction_completed"):
            self.assertIn(key, rerolled)

    def test_frontend_javascript_syntax(self):
        script = Path(self.temp_directory.name) / "index-script.js"
        html = Path(api.__file__).with_name("index.html").read_text(encoding="utf-8")
        source = html.split("<script>", 1)[1].split("</script>", 1)[0]
        script.write_text(source, encoding="utf-8")
        completed = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class LegacyPlayerMigrationIntegrationTest(unittest.TestCase):
    def test_old_schema_player_gets_all_loot_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "legacy.db")
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE players (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, level INTEGER NOT NULL DEFAULT 1, gold INTEGER NOT NULL DEFAULT 0, enemy_hp INTEGER NOT NULL DEFAULT 30, updated_at INTEGER NOT NULL)")
            connection.execute("INSERT INTO players VALUES (1,'legacy','Legacy',1,0,30,0)")
            connection.commit(); connection.close()
            with patch.object(api, "DATABASE_PATH", database):
                api.create_database(); api.create_database()
                connection = api.get_database()
                player = api.load_player(connection, 1)
                connection.close()
            self.assertEqual(player["salvage_dust"], 0)
            self.assertEqual(player["refinement_crystal"], 0)
            self.assertEqual(player["refinement_ore"], 0)
            self.assertEqual(player["premium_crystals"], 0)
            self.assertEqual(player["chest_xp"], 0)
            self.assertEqual(json.loads(player["inventory_json"]), [])
            self.assertEqual(player["auto_salvage_max_rarity"], "off")


if __name__ == "__main__":
    unittest.main()
