import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api


class DamageTypeIntegrationTest(unittest.TestCase):
    telegram_id = 830001

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_directory.name) / "damage-integration.db")
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.auth_patch = patch.object(api, "validate_telegram_data", return_value={
            "id": self.telegram_id, "username": "damage", "first_name": "Damage",
        })
        self.database_patch.start()
        self.auth_patch.start()
        api.create_database()
        api.get_or_create_player(api.validate_telegram_data("test"))
        self.player_patch = patch.object(api, "get_or_create_player", side_effect=self.load_player)
        self.player_patch.start()
        api.BATTLE_STATES.clear()

    def tearDown(self):
        api.BATTLE_STATES.clear()
        self.player_patch.stop()
        self.auth_patch.stop()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def load_player(self, *_args, **_kwargs):
        connection = api.get_database()
        try:
            return api.load_player(connection, self.telegram_id)
        finally:
            connection.close()

    def set_state(self, **values):
        defaults = {
            "level": 50,
            "experience": api.LEVEL_TOTAL_EXP[50],
            "damage": 100,
            "hero_hp": 688,
            "hero_max_hp": 688,
            "enemy_hp": 10,
            "enemy_max_hp": 1000,
            "last_attack_at": 0,
            "last_enemy_attack_at": 0,
            "skills_auto_enabled": 0,
            "kills_in_stage": 0,
            "boss_active": 0,
            "boss_waiting": 0,
            "enemy_archetype": "",
            "enemy_resistances_json": "",
            "skills_collection_json": json.dumps({
                "thorn_burst": {"owned": True, "level": 1, "fragments": 0},
                "arcane_echo": {"owned": True, "level": 1, "fragments": 0},
            }),
            "skill_slots_json": json.dumps(["thorn_burst", "arcane_echo", None]),
            "companions_collection_json": "{}",
            "companion_slots_json": "[]",
            "thorn_burst_last_used_at": 0,
            "arcane_echo_last_used_at": 0,
        }
        defaults.update(values)
        assignments = ", ".join(f"{key} = ?" for key in defaults)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            f"UPDATE players SET {assignments} WHERE telegram_id = ?",
            (*defaults.values(), self.telegram_id),
        )
        connection.commit()
        connection.close()

    def attack(self, skill=None, battle_id=None):
        with patch.object(api.random, "random", return_value=1.0):
            return api.attack("test", skill=skill, battle_id=battle_id)

    def test_thorn_burst_lethal_rewards_and_stale_retry_once(self):
        self.set_state()
        before = self.load_player()
        old_identity = api.public_battle_identity(before)
        old_chests = int(before["chests"])

        result = self.attack("thorn_burst", old_identity)
        retry = self.attack("thorn_burst", old_identity)

        self.assertTrue(result["combat_resolution"]["reward_granted"])
        self.assertEqual(result["combat_resolution"]["lethal_source"], "thorn_burst")
        self.assertEqual(sum(event["reward_granted"] for event in result["combat_resolution"]["events"]), 1)
        self.assertNotEqual(result["stage_sequence"]["battle_identity"], old_identity)
        self.assertEqual(result["chests"] - old_chests, 2)
        self.assertTrue(retry["stale_battle"])
        self.assertFalse(retry["attacked"])
        self.assertEqual(retry["chests"], result["chests"])
        self.assertEqual(retry["total_kills"], result["total_kills"])

    def test_arcane_echo_first_lethal_skips_second_and_stale_retry(self):
        self.set_state(enemy_hp=10)
        old_identity = api.public_battle_identity(self.load_player())

        result = self.attack("arcane_echo", old_identity)
        events = result["combat_resolution"]["events"]
        retry = self.attack("arcane_echo", old_identity)

        self.assertEqual(events[0]["damage_source"], "arcane_echo")
        self.assertTrue(events[0]["lethal"])
        self.assertEqual(events[1]["damage_source"], "arcane_echo_hit_2")
        self.assertTrue(events[1]["skipped_after_kill"])
        self.assertEqual(events[1]["final_damage"], 0)
        self.assertEqual(sum(event["reward_granted"] for event in events), 1)
        self.assertEqual(result["combat_resolution"]["lethal_source"], "arcane_echo")
        self.assertTrue(retry["stale_battle"])
        self.assertEqual(retry["total_kills"], result["total_kills"])
        self.assertEqual(retry["chests"], result["chests"])

    def test_crystal_moth_finishes_surviving_normal_attack(self):
        collection = json.dumps({
            "crystal_moth": {"owned": True, "level": 1, "fragments": 0},
        })
        self.set_state(
            enemy_hp=101,
            companions_collection_json=collection,
            companion_slots_json=json.dumps(["crystal_moth", None, None]),
        )
        before = self.load_player()
        old_identity = api.public_battle_identity(before)

        result = self.attack(battle_id=old_identity)
        events = result["combat_resolution"]["events"]
        normal = next(event for event in events if event["damage_source"] == "normal")
        moth = next(event for event in events if event["damage_source"] == "crystal_moth")
        retry = self.attack(battle_id=old_identity)

        self.assertFalse(normal["lethal"])
        self.assertTrue(moth["lethal"])
        self.assertEqual(moth["damage_type"], "arcane")
        self.assertEqual(result["combat_resolution"]["lethal_source"], "crystal_moth")
        self.assertEqual(sum(event["reward_granted"] for event in events), 1)
        self.assertEqual(result["hero_hp"], api.build_player_response(before)["hero_hp"])
        self.assertEqual(result.get("received_damage", 0), 0)
        self.assertTrue(retry["stale_battle"])
        self.assertEqual(retry["total_kills"], result["total_kills"])

    def test_thorn_burst_victory_failure_rolls_back_then_rewards_once(self):
        self.set_state(enemy_hp=10)
        before = self.load_player()
        old_identity = api.public_battle_identity(before)
        original_victory = api._resolve_victory

        def fail_after_victory(*args, **kwargs):
            original_victory(*args, **kwargs)
            raise RuntimeError("forced victory rollback")

        with patch.object(api, "_resolve_victory", side_effect=fail_after_victory):
            with self.assertRaisesRegex(RuntimeError, "forced victory rollback"):
                self.attack("thorn_burst", old_identity)

        rolled_back = self.load_player()
        self.assertEqual(rolled_back["enemy_hp"], before["enemy_hp"])
        self.assertEqual(rolled_back["chests"], before["chests"])
        self.assertEqual(rolled_back["total_kills"], before["total_kills"])
        self.assertEqual(api.public_battle_identity(rolled_back), old_identity)

        result = self.attack("thorn_burst", old_identity)
        persisted = self.load_player()
        self.assertTrue(result["enemy_defeated"])
        self.assertEqual(sum(event["reward_granted"] for event in result["combat_resolution"]["events"]), 1)
        self.assertEqual(persisted["total_kills"], before["total_kills"] + 1)
        self.assertEqual(persisted["chests"], before["chests"] + 2)

    def test_legacy_enemy_is_neutral_and_next_spawn_has_profile(self):
        self.set_state(enemy_hp=10, enemy_archetype="", enemy_resistances_json="")
        legacy = self.load_player()
        legacy_identity = api.public_battle_identity(legacy)

        public_legacy = api.build_player_response(legacy)
        self.assertEqual(public_legacy["resistances"], {
            "physical": 0.0, "nature": 0.0, "poison": 0.0, "arcane": 0.0,
        })
        self.assertIn("enemy_archetype", public_legacy)
        self.assertIn("enemy_attack_type", public_legacy)

        result = self.attack("thorn_burst", legacy_identity)
        spawned = self.load_player()
        stored_resistances = json.loads(spawned["enemy_resistances_json"])

        self.assertTrue(result["enemy_defeated"])
        self.assertTrue(spawned["enemy_archetype"])
        self.assertEqual(set(stored_resistances), {"physical", "nature", "poison", "arcane"})
        self.assertTrue(result["enemy_archetype"])
        self.assertIn(result["enemy_attack_type"], {"physical", "nature", "poison", "arcane"})


if __name__ == "__main__":
    unittest.main()
