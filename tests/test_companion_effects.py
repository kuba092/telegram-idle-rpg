import json
import sqlite3
import tempfile
import time
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
        collection = self.collection(forest_sprite=20)
        self.set_state(collection, ["forest_sprite", None, None])

        response = self.attack()

        self.assertEqual(response["damage_dealt"], 126)
        self.assertEqual(response["companion_effects"]["damage_multiplier"], 1.26)

        self.set_state(collection, ["forest_sprite", None, None])
        skill_response = self.attack("spore_strike")
        self.assertEqual(
            skill_response["damage_dealt"],
            round(100 * api.SPORE_STRIKE_DAMAGE_MULTIPLIER * 1.26),
        )

    def test_baby_slime_hp_bonus(self):
        collection = self.collection(baby_slime=20)
        self.set_state(collection, ["baby_slime", None, None])

        response = api.build_player_response(self.player())

        self.assertEqual(response["hero_max_hp"], 130)
        self.assertEqual(response["hero_hp"], 65)
        self.assertEqual(response["hero_stats"]["total"]["hero_max_hp"], 130)

    def test_hp_ratio_is_preserved_when_equipping_and_unequipping(self):
        collection = self.collection(baby_slime=20)
        self.set_state(collection, [None, None, None])

        equipped = api.equip_companion(1, "baby_slime", "test")
        unequipped = api.unequip_companion(1, "test")

        self.assertEqual((equipped["hero_hp"], equipped["hero_max_hp"]), (65, 130))
        self.assertEqual((unequipped["hero_hp"], unequipped["hero_max_hp"]), (50, 100))

    def test_spore_beetle_extra_attack_damage(self):
        collection = self.collection(spore_beetle=20)
        self.set_state(collection, ["spore_beetle", None, None])

        response = self.attack()

        self.assertEqual(response["companion_damage"], 56)
        self.assertEqual(response["damage_dealt"], 156)

    def test_unequipped_companion_has_no_effect(self):
        collection = self.collection(forest_sprite=5, baby_slime=5, spore_beetle=5)
        self.set_state(collection, [None, None, None])

        response = api.build_player_response(self.player())

        self.assertEqual(response["companion_effects"]["damage_multiplier"], 1.0)
        self.assertEqual(response["companion_effects"]["hp_multiplier"], 1.0)
        self.assertEqual(response["companion_effects"]["extra_attack_damage"], 0)
        self.assertEqual(response["companion_effects"]["skill_cooldown_multiplier"], 1.0)
        self.assertEqual(response["companion_effects"]["crit_chance_bonus"], 0.0)
        self.assertEqual(response["companion_effects"]["crit_damage_bonus"], 0.0)
        self.assertEqual(response["companion_effects"]["active_effects"], [])

    def test_multiple_companion_effects_are_combined(self):
        collection = self.collection(forest_sprite=2, baby_slime=2, spore_beetle=3)
        self.set_state(collection, ["forest_sprite", "baby_slime", "spore_beetle"])

        before_attack = api.build_player_response(self.player())
        response = self.attack()

        self.assertEqual(response["damage_dealt"], 111)
        self.assertEqual(response["companion_damage"], 8)
        self.assertEqual(before_attack["hero_max_hp"], 103)
        self.assertEqual(len(response["companion_effects"]["active_effects"]), 3)

    def test_mushroom_owl_reduces_skill_cooldown(self):
        collection = self.collection(mushroom_owl=10)
        self.set_state(
            collection,
            ["mushroom_owl", None, None],
            spore_strike_last_used_at=time.time(),
        )

        response = api.build_player_response(self.player())

        self.assertEqual(response["companion_effects"]["skill_cooldown_multiplier"], 0.85)
        self.assertEqual(response["skills"]["spore_strike"]["cooldown_seconds"], 6.8)
        self.assertLessEqual(response["skills"]["spore_strike"]["cooldown_remaining"], 6.8)

        self.set_state(
            collection,
            ["mushroom_owl", None, None],
            spore_strike_last_used_at=time.time() - 7.5,
        )
        skill_response = self.attack("spore_strike")
        self.assertTrue(skill_response["attacked"])
        self.assertEqual(skill_response["skill_used"], "spore_strike")

    def test_mushroom_owl_cooldown_reduction_is_capped(self):
        collection = self.collection(mushroom_owl=20)
        self.set_state(collection, ["mushroom_owl", None, None])

        effects = api.calculate_companion_effects(self.player())

        self.assertEqual(effects["skill_cooldown_multiplier"], 0.7)

    def test_thorn_wolf_increases_and_caps_critical_chance(self):
        collection = self.collection(thorn_wolf=20)
        self.set_state(collection, ["thorn_wolf", None, None])
        effects = api.calculate_companion_effects(self.player())
        self.assertEqual(effects["crit_chance_bonus"], 15.0)
        self.assertEqual(effects["crit_damage_bonus"], 20.0)
        self.assertEqual(effects["active_effects"][0]["crit_damage_bonus"], 20.0)

        fake_stats = {"total": {"crit_chance": 99.0, "crit_damage": 200.0}}
        with (
            patch.object(api, "calculate_equipment_stats", return_value=fake_stats),
            patch.object(api.random, "random", return_value=0.9999),
        ):
            damage, critical = api.calculate_hero_attack_damage(self.player())

        self.assertTrue(critical)
        self.assertEqual(damage, 220)

    def test_thorn_wolf_critical_bonus_applies_to_attack_and_skill(self):
        collection = self.collection(thorn_wolf=10)
        self.set_state(collection, ["thorn_wolf", None, None])
        with patch.object(api.random, "random", return_value=0.10):
            normal_response = api.attack("test")

        self.set_state(collection, ["thorn_wolf", None, None])
        with patch.object(api.random, "random", return_value=0.10):
            skill_response = api.attack("test", skill="spore_strike")

        self.assertTrue(normal_response["critical"])
        self.assertTrue(skill_response["critical"])
        self.assertEqual(normal_response["damage_dealt"], 185)
        self.assertEqual(skill_response["damage_dealt"], 370)

    def test_ancient_entling_heals_after_normal_enemy(self):
        collection = self.collection(ancient_entling=20)
        self.set_state(
            collection,
            ["ancient_entling", None, None],
            enemy_hp=1,
            hero_hp=344,
            hero_max_hp=688,
        )

        response = self.attack()

        self.assertEqual(response["companion_healing"], 83)
        self.assertEqual(response["hero_hp"], 427)

    def test_ancient_entling_heals_twice_as_much_after_boss(self):
        collection = self.collection(ancient_entling=20)
        self.set_state(
            collection,
            ["ancient_entling", None, None],
            enemy_hp=1,
            boss_active=1,
            hero_hp=344,
            hero_max_hp=688,
        )

        response = self.attack()

        self.assertTrue(response["boss_defeated"])
        self.assertEqual(response["companion_healing"], 165)
        self.assertEqual(response["hero_hp"], 509)

    def test_ancient_entling_healing_does_not_exceed_max_hp(self):
        collection = self.collection(ancient_entling=20)
        self.set_state(
            collection,
            ["ancient_entling", None, None],
            enemy_hp=1,
            hero_hp=683,
            hero_max_hp=688,
        )

        response = self.attack()

        self.assertEqual(response["companion_healing"], 5)
        self.assertEqual(response["hero_hp"], 688)

    def test_closed_slot_does_not_apply_new_effect(self):
        collection = self.collection(mushroom_owl=5, thorn_wolf=5)
        self.set_state(
            collection,
            ["mushroom_owl", "thorn_wolf", None],
            level=10,
            experience=api.LEVEL_TOTAL_EXP[10],
        )

        effects = api.calculate_companion_effects(self.player())

        self.assertEqual(effects["skill_cooldown_multiplier"], 0.925)
        self.assertEqual(effects["crit_chance_bonus"], 0.0)

    def test_multiple_new_effects_work_together(self):
        collection = self.collection(
            mushroom_owl=5, thorn_wolf=4, ancient_entling=5
        )
        self.set_state(
            collection,
            ["mushroom_owl", "thorn_wolf", "ancient_entling"],
            enemy_hp=1,
            hero_hp=344,
            hero_max_hp=688,
        )

        response = self.attack()

        self.assertEqual(response["companion_effects"]["skill_cooldown_multiplier"], 0.925)
        self.assertEqual(response["companion_effects"]["crit_chance_bonus"], 3.0)
        self.assertEqual(response["companion_effects"]["crit_damage_bonus"], 4.0)
        self.assertEqual(response["companion_healing"], 21)
        self.assertEqual(len(response["companion_effects"]["active_effects"]), 3)

    def test_mushroom_shield_uses_thirty_percent_base_hp(self):
        self.set_state(self.collection(), [None, None, None])

        response = api.build_player_response(self.player())

        self.assertEqual(response["skills"]["mushroom_shield"]["hp_ratio"], 0.3)
        self.assertEqual(response["skills"]["mushroom_shield"]["capacity"], 30)

    def test_poison_cloud_uses_forty_five_percent_damage_per_tick(self):
        self.set_state(self.collection(), [None, None, None])

        response = api.build_player_response(self.player())

        self.assertEqual(response["skills"]["poison_cloud"]["damage_multiplier"], 0.45)


if __name__ == "__main__":
    unittest.main()
