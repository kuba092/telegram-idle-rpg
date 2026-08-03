import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api
from progression_systems import companion_milestone_multiplier


class StageSequenceTest(unittest.TestCase):
    telegram_id = 810001

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_directory.name) / "stage.db")
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.auth_patch = patch.object(api, "validate_telegram_data", return_value={
            "id": self.telegram_id, "username": "stage", "first_name": "Stage",
        })
        self.database_patch.start()
        self.auth_patch.start()
        api.create_database()
        api.get_or_create_player(api.validate_telegram_data("test"))
        self.player_patch = patch.object(
            api, "get_or_create_player", side_effect=self.load_player
        )
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
            "hero_hp": 344,
            "hero_max_hp": 688,
            "enemy_hp": 1,
            "enemy_max_hp": 30,
            "last_attack_at": 0,
            "mushroom_shield_amount": 25,
            "mushroom_shield_last_used_at": 123,
            "spore_strike_last_used_at": 123,
            "poison_cloud_last_used_at": 123,
            "poison_cloud_until": 9999999999,
            "poison_cloud_next_tick_at": 9999999998,
            "skills_auto_enabled": 0,
            "companions_collection_json": "{}",
            "companion_slots_json": "[]",
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

    def attack(self):
        with patch.object(api.random, "random", return_value=1.0):
            return api.attack("test")

    def test_normal_victory_carries_hp_and_clears_temporary_state(self):
        self.set_state()
        old = self.load_player()
        state = api.BATTLE_STATES.get(self.telegram_id, api.battle_identity(old), now=1)
        api.BATTLE_STATES.use_skill(state, "spore_strike", True)
        api.BATTLE_STATES.begin_poison(state, True)
        api.BATTLE_STATES.break_shield(state, 1)

        response = self.attack()

        self.assertEqual(response["hero_hp"], 344)
        self.assertEqual(response["stage_sequence"]["hp_carried_to_next_battle"], 344)
        self.assertTrue(response["stage_sequence"]["temporary_effects_cleared"])
        self.assertTrue(response["stage_sequence"]["cooldowns_reset_between_battles"])
        self.assertEqual(response["skills"]["mushroom_shield"]["amount"], 0)
        self.assertFalse(response["skills"]["poison_cloud"]["active"])
        self.assertEqual(response["battle_effects"]["owl_repeat_stacks"], {})
        self.assertEqual(response["battle_effects"]["poison_completion_stacks"], 0)
        self.assertFalse(response["battle_effects"]["shield_mitigation_active"])
        self.assertIsNone(api.BATTLE_STATES.peek(
            self.telegram_id, api.battle_identity(old), now=2
        ))
        self.assertEqual(response["skills"]["spore_strike"]["cooldown_remaining"], 0)

    def test_tenth_enemy_carries_hp_to_boss_with_new_identity(self):
        self.set_state(kills_in_stage=9)
        before = self.load_player()
        response = self.attack()
        after = self.load_player()

        self.assertTrue(response["boss_active"])
        self.assertEqual(response["stage_sequence"]["current_hp"], 344)
        self.assertEqual(response["stage_sequence"]["enemies_remaining_before_boss"], 0)
        self.assertNotEqual(api.battle_identity(before), api.battle_identity(after))

    def test_boss_carries_hp_to_next_stage(self):
        self.set_state(stage=3, kills_in_stage=10, boss_active=1)
        response = self.attack()

        self.assertTrue(response["boss_defeated"])
        self.assertEqual(response["stage"], 4)
        self.assertEqual(response["hero_hp"], 344)
        self.assertEqual(response["stage_sequence"]["hp_carried_to_next_battle"], 344)

    def test_entling_healing_snapshot_overflow_and_read_idempotency(self):
        collection = json.dumps({
            "ancient_entling": {"owned": True, "level": 20, "fragments": 0}
        })
        self.set_state(
            hero_hp=683,
            companions_collection_json=collection,
            companion_slots_json=json.dumps(["ancient_entling", None, None]),
        )
        response = self.attack()
        read_again = api.player("test")

        self.assertEqual(response["stage_sequence"]["hp_before_healing"], 683)
        self.assertEqual(response["companion_healing"], 5)
        healing = round(688 * .006 * 20 * companion_milestone_multiplier(20))
        self.assertEqual(response["healing_overflow"], healing - 5)
        self.assertEqual(response["stage_sequence"]["hp_after_healing"], 688)
        self.assertEqual(read_again["hero_hp"], 688)
        self.assertEqual(read_again["stage_sequence"]["companion_healing"], 0)

    def test_unequipped_and_closed_slot_entling_do_not_heal(self):
        collection = json.dumps({
            "ancient_entling": {"owned": True, "level": 20, "fragments": 0}
        })
        self.set_state(companions_collection_json=collection, companion_slots_json="[]")
        self.assertEqual(self.attack()["companion_healing"], 0)

        self.set_state(
            level=10,
            experience=api.LEVEL_TOTAL_EXP[10],
            companions_collection_json=collection,
            companion_slots_json=json.dumps([None, "ancient_entling", None]),
        )
        self.assertEqual(self.attack()["companion_healing"], 0)

    def test_store_restart_keeps_database_hp_and_player_reports_it(self):
        self.set_state(hero_hp=37, enemy_hp=1000, enemy_max_hp=1000)
        identity = api.battle_identity(self.load_player())
        api.BATTLE_STATES.get(self.telegram_id, identity, now=1)
        api.BATTLE_STATES.clear()

        response = api.player("test")

        self.assertEqual(response["hero_hp"], 37)
        self.assertEqual(response["stage_sequence"]["current_hp"], 37)
        self.assertIsNone(api.BATTLE_STATES.peek(self.telegram_id, identity, now=2))

    def test_same_enemy_identity_is_stable_and_players_are_isolated(self):
        player = self.load_player()
        identity = api.battle_identity(player)
        first = api.BATTLE_STATES.get(self.telegram_id, identity, now=1)
        second = api.BATTLE_STATES.get(self.telegram_id, identity, now=2)
        other = api.BATTLE_STATES.get(self.telegram_id + 1, identity, now=2)

        self.assertIs(first, second)
        self.assertIsNot(first, other)

    def test_defeat_ends_in_memory_battle_and_clears_active_effects(self):
        self.set_state(
            hero_hp=1,
            enemy_hp=1000,
            enemy_max_hp=1000,
            last_enemy_attack_at=0,
            mushroom_shield_amount=0,
        )
        identity = api.battle_identity(self.load_player())
        state = api.BATTLE_STATES.get(self.telegram_id, identity, now=1)
        api.BATTLE_STATES.begin_poison(state, True)

        response = api.enemy_attack("test")

        self.assertTrue(response["hero_defeated"])
        self.assertEqual(response["hero_hp"], 0)
        self.assertFalse(response["skills"]["poison_cloud"]["active"])
        self.assertEqual(response["skills"]["mushroom_shield"]["amount"], 0)
        self.assertIsNone(api.BATTLE_STATES.peek(self.telegram_id, identity))

    def test_stale_retry_cannot_reward_or_heal_next_enemy(self):
        collection = json.dumps({
            "ancient_entling": {"owned": True, "level": 20, "fragments": 0}
        })
        self.set_state(
            companions_collection_json=collection,
            companion_slots_json=json.dumps(["ancient_entling", None, None]),
        )
        old_identity = api.public_battle_identity(self.load_player())
        first = api.attack("test", battle_id=old_identity)
        retry = api.attack("test", battle_id=old_identity)

        self.assertTrue(first["enemy_defeated"])
        self.assertFalse(retry["attacked"])
        self.assertTrue(retry["stale_battle"])
        self.assertEqual(retry["stage_sequence"]["companion_healing"], 0)
        self.assertEqual(retry["total_kills"], first["total_kills"])


if __name__ == "__main__":
    unittest.main()
