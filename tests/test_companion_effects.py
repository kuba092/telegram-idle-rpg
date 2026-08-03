import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api


class CompanionEffectsTest(unittest.TestCase):
    telegram_id = 700002

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_directory.name) / "game-test.db")
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.auth_patch = patch.object(
            api,
            "validate_telegram_data",
            return_value={
                "id": self.telegram_id,
                "username": "companion_effect_test",
                "first_name": "Test",
            },
        )
        self.database_patch.start()
        self.auth_patch.start()
        api.create_database()
        api.get_or_create_player(api.validate_telegram_data("test"))
        self.player_patch = patch.object(
            api,
            "get_or_create_player",
            return_value={"telegram_id": self.telegram_id},
        )
        self.player_patch.start()

    def tearDown(self):
        self.player_patch.stop()
        self.auth_patch.stop()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def set_state(self, collection, slots, **values):
        state = {
            "level": 50,
            "experience": api.LEVEL_TOTAL_EXP[50],
            "damage": 100,
            "hero_hp": 50,
            "hero_max_hp": 100,
            "enemy_hp": 10000,
            "enemy_max_hp": 10000,
            "last_attack_at": 0,
            "skills_auto_enabled": 0,
            "skills_collection_json": json.dumps({
                "spore_strike": {
                    "owned": True,
                    "level": 1,
                    "fragments": 0,
                }
            }),
            "skill_slots_json": json.dumps(["spore_strike", None, None]),
            "companions_collection_json": json.dumps(collection),
            "companion_slots_json": json.dumps(slots),
        }
        state.update(values)
        assignments = ", ".join(f"{key} = ?" for key in state)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            f"UPDATE players SET {assignments} WHERE telegram_id = ?",
            (*state.values(), self.telegram_id),
        )
        connection.commit()
        connection.close()

    @staticmethod
    def collection(**levels):
        return {
            companion_id: {
                "owned": True,
                "level": level,
                "fragments": 0,
            }
            for companion_id, level in levels.items()
        }

    def player(self):
        connection = api.get_database()
        try:
            return api.load_player(connection, self.telegram_id)
        finally:
            connection.close()

    def attack(self, skill=None):
        with patch.object(api.random, "random", return_value=1.0):
            return api.attack(
                x_telegram_init_data="test",
                skill=skill,
            )

    def test_forest_sprite_damage_bonus(self):
        collection = self.collection(forest_sprite=2)
        self.set_state(collection, ["forest_sprite", None, None])

        response = self.attack()

        self.assertEqual(response["damage_dealt"], 106)
        self.assertEqual(response["companion_effects"]["damage_multiplier"], 1.06)

        self.set_state(collection, ["forest_sprite", None, None])
        skill_response = self.attack("spore_strike")
        self.assertEqual(
            skill_response["damage_dealt"],
            round(100 * api.SPORE_STRIKE_DAMAGE_MULTIPLIER * 1.06),
        )

    def test_baby_slime_hp_bonus(self):
        collection = self.collection(baby_slime=2)
        self.set_state(collection, ["baby_slime", None, None])

        response = api.build_player_response(self.player())

        self.assertEqual(response["hero_max_hp"], 110)
        self.assertEqual(response["hero_hp"], 55)
        self.assertEqual(response["hero_stats"]["total"]["hero_max_hp"], 110)

    def test_hp_ratio_is_preserved_when_equipping_and_unequipping(self):
        collection = self.collection(baby_slime=2)
        self.set_state(collection, [None, None, None])

        equipped = api.equip_companion(1, "baby_slime", "test")
        unequipped = api.unequip_companion(1, "test")

        self.assertEqual((equipped["hero_hp"], equipped["hero_max_hp"]), (55, 110))
        self.assertEqual((unequipped["hero_hp"], unequipped["hero_max_hp"]), (50, 100))

    def test_spore_beetle_extra_attack_damage(self):
        collection = self.collection(spore_beetle=3)
        self.set_state(collection, ["spore_beetle", None, None])

        response = self.attack()

        self.assertEqual(response["companion_damage"], 6)
        self.assertEqual(response["damage_dealt"], 106)

    def test_unequipped_companion_has_no_effect(self):
        collection = self.collection(forest_sprite=5, baby_slime=5, spore_beetle=5)
        self.set_state(collection, [None, None, None])

        response = api.build_player_response(self.player())

        self.assertEqual(response["companion_effects"]["damage_multiplier"], 1.0)
        self.assertEqual(response["companion_effects"]["hp_multiplier"], 1.0)
        self.assertEqual(response["companion_effects"]["extra_attack_damage"], 0)
        self.assertEqual(response["companion_effects"]["active_effects"], [])

    def test_multiple_companion_effects_are_combined(self):
        collection = self.collection(forest_sprite=2, baby_slime=2, spore_beetle=3)
        self.set_state(collection, ["forest_sprite", "baby_slime", "spore_beetle"])

        before_attack = api.build_player_response(self.player())
        response = self.attack()

        self.assertEqual(response["damage_dealt"], 112)
        self.assertEqual(response["companion_damage"], 6)
        self.assertEqual(before_attack["hero_max_hp"], 110)
        self.assertEqual(len(response["companion_effects"]["active_effects"]), 3)


if __name__ == "__main__":
    unittest.main()
