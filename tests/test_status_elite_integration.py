import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api
from combat_effects import CombatContext
from combat_resolver import CombatResolver


class StatusEliteIntegrationTest(unittest.TestCase):
    telegram_id = 940001

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = str(Path(self.temp_directory.name) / "status-elite.db")
        self.database_patch = patch.object(api, "DATABASE_PATH", self.database_path)
        self.auth_patch = patch.object(api, "validate_telegram_data", return_value={
            "id": self.telegram_id, "username": "status", "first_name": "Status",
        })
        self.database_patch.start(); self.auth_patch.start()
        api.create_database()
        api.get_or_create_player(api.validate_telegram_data("test"))
        self.player_patch = patch.object(api, "get_or_create_player", side_effect=self.load_player)
        self.player_patch.start()
        api.BATTLE_STATES.clear(); api.STATUS_EFFECTS.clear()

    def tearDown(self):
        api.BATTLE_STATES.clear(); api.STATUS_EFFECTS.clear()
        self.player_patch.stop(); self.auth_patch.stop(); self.database_patch.stop()
        self.temp_directory.cleanup()

    def load_player(self, *_args, **_kwargs):
        connection = api.get_database()
        try:
            return api.load_player(connection, self.telegram_id)
        finally:
            connection.close()

    def set_state(self, **values):
        skills = {
            skill_id: {"owned": True, "level": 1, "fragments": 0}
            for skill_id in ("poison_cloud", "venom_spores", "binding_roots",
                             "null_bloom", "thorn_burst", "mushroom_shield")
        }
        defaults = {
            "level": 50, "experience": api.LEVEL_TOTAL_EXP[50], "damage": 100,
            "hero_hp": 688, "hero_max_hp": 688, "enemy_hp": 1000,
            "enemy_max_hp": 1000, "stage": 10, "kills_in_stage": 0,
            "last_attack_at": 0, "last_enemy_attack_at": 0,
            "skills_auto_enabled": 0, "boss_active": 0, "boss_waiting": 0,
            "enemy_archetype": "", "enemy_resistances_json": "",
            "skills_collection_json": json.dumps(skills),
            "skill_slots_json": json.dumps(["poison_cloud", "binding_roots", "null_bloom"]),
            "companions_collection_json": "{}", "companion_slots_json": "[]",
            "poison_cloud_last_used_at": 0, "poison_cloud_until": 0,
            "poison_cloud_next_tick_at": 0, "venom_spores_last_used_at": 0,
            "binding_roots_last_used_at": 0, "null_bloom_last_used_at": 0,
            "thorn_burst_last_used_at": 0, "mushroom_shield_last_used_at": 0,
        }
        defaults.update(values)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            f"UPDATE players SET {', '.join(f'{key}=?' for key in defaults)} WHERE telegram_id=?",
            (*defaults.values(), self.telegram_id),
        )
        connection.commit(); connection.close()

    def update(self, **values):
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            f"UPDATE players SET {', '.join(f'{key}=?' for key in values)} WHERE telegram_id=?",
            (*values.values(), self.telegram_id),
        )
        connection.commit(); connection.close()

    def attack(self, skill=None, battle_id=None, now=100.0):
        with patch.object(api.time, "time", return_value=now), patch.object(api.random, "random", return_value=1.0):
            return api.attack("test", skill=skill, battle_id=battle_id)

    def enemy_attack(self, now=100.0):
        with patch.object(api.time, "time", return_value=now), patch.object(api.random, "random", return_value=1.0):
            return api.enemy_attack("test")

    def use_poison(self, now=100.0):
        with patch.object(api.time, "time", return_value=now):
            return api.use_poison_cloud("test")

    def status(self, effect_type, source_id, now, duration, **values):
        return api.apply_status(self.load_player(), effect_type=effect_type, source_id=source_id,
                                now=now, duration=duration, **values).effect

    def equip(self, *skill_ids):
        self.update(skill_slots_json=json.dumps([*skill_ids, *([None] * (3-len(skill_ids)))]))

    def test_poison_cloud_lethal_tick_rewards_once_and_clears_next_enemy(self):
        self.set_state(enemy_hp=10)
        identity = api.public_battle_identity(self.load_player())
        used = self.use_poison(100)
        self.assertEqual(used["status_effects"]["active"][0]["source_id"], "poison_cloud")
        result = self.attack(battle_id=identity, now=101.1)
        events = result["combat_resolution"]["events"]
        tick = next(event for event in events if event["damage_source"] == "poison_cloud")
        self.assertTrue(tick["lethal"]); self.assertEqual(tick["tick_index"], 1)
        self.assertTrue(next(event for event in events if event["damage_source"] == "normal")["skipped_after_kill"])
        self.assertEqual(sum(event["reward_granted"] for event in events), 1)
        retry = self.attack(battle_id=identity, now=102.2)
        self.assertTrue(retry["stale_battle"])
        self.assertEqual(retry["total_kills"], result["total_kills"])
        self.assertFalse(any(e["source_id"] == "poison_cloud" for e in result["status_effects"]["active"]))

    def test_poison_tick_victory_rollback_restores_claim_and_store(self):
        self.set_state(enemy_hp=10)
        identity = api.public_battle_identity(self.load_player())
        self.use_poison(100)
        before = self.load_player(); original = api._resolve_victory
        processed_before = api.STATUS_EFFECTS.list(self.telegram_id, identity, 100.5)[0].processed_ticks
        with patch.object(api, "_resolve_victory", side_effect=lambda *args: (original(*args), (_ for _ in ()).throw(RuntimeError("dot rollback")))[0]):
            with self.assertRaisesRegex(RuntimeError, "dot rollback"):
                self.attack(battle_id=identity, now=101.1)
        rolled = self.load_player()
        self.assertEqual((rolled["enemy_hp"], rolled["chests"], rolled["stage"]),
                         (before["enemy_hp"], before["chests"], before["stage"]))
        self.assertEqual(api.public_battle_identity(rolled), identity)
        restored = api.STATUS_EFFECTS.list(self.telegram_id, identity, 101.1)[0]
        self.assertEqual(restored.processed_ticks, processed_before)
        result = self.attack(battle_id=identity, now=101.1)
        self.assertEqual(sum(e["reward_granted"] for e in result["combat_resolution"]["events"]), 1)

    def test_venom_instances_replace_oldest_and_keep_snapshots(self):
        self.set_state(); self.equip("venom_spores")
        first = self.attack("venom_spores", now=100)
        self.update(last_attack_at=0, venom_spores_last_used_at=0, damage=200)
        second = self.attack("venom_spores", now=101)
        self.update(last_attack_at=0, venom_spores_last_used_at=0, damage=300)
        third = self.attack("venom_spores", now=102)
        active = [e for e in api.STATUS_EFFECTS.list(self.telegram_id, api.public_battle_identity(self.load_player()), 102.1)
                  if e.source_id == "venom_spores"]
        self.assertEqual(len(active), 2)
        self.assertEqual([e.snapshot["base_damage"] for e in active], [200, 300])
        self.assertNotEqual(active[0].effect_id, active[1].effect_id)
        self.assertEqual(api.public_battle_effects(self.load_player())["poison_completion_stacks"], 0)

    def test_venom_lethal_tick_stops_other_events(self):
        self.set_state(enemy_hp=20); self.equip("venom_spores")
        self.attack("venom_spores", now=100)
        self.update(last_attack_at=0, venom_spores_last_used_at=0)
        self.attack("venom_spores", now=100.1)
        identity = api.public_battle_identity(self.load_player())
        self.update(last_attack_at=0)
        result = self.attack(battle_id=identity, now=101.2)
        events = result["combat_resolution"]["events"]
        self.assertTrue(events[0]["lethal"])
        self.assertTrue(all(event["skipped_after_kill"] for event in events[1:]))

    def test_binding_roots_nonlethal_lethal_and_enemy_block(self):
        self.set_state(); self.equip("binding_roots")
        rooted = self.attack("binding_roots", now=100)
        self.assertTrue(any(e["effect_type"] == "root" for e in rooted["status_effects"]["active"]))
        hero_before = rooted["hero_hp"]
        blocked = self.enemy_attack(100.5)
        self.assertTrue(blocked["blocked"]); self.assertEqual(blocked["hero_hp"], hero_before)
        self.assertFalse(blocked["counter_triggered"])
        api.STATUS_EFFECTS.clear(); self.set_state(enemy_hp=10); self.equip("binding_roots")
        lethal = self.attack("binding_roots", now=200)
        self.assertFalse(any(e["effect_type"] == "root" for e in lethal["status_effects"]["active"]))

    def test_root_does_not_stop_due_enemy_dot(self):
        self.set_state(enemy_hp=100); identity = api.public_battle_identity(self.load_player())
        self.status("root", "test_root", 100, 5, control_flags={"blocks_enemy_attack": True})
        self.status("damage_over_time", "test_dot", 100, 5, damage_type="poison",
                    max_ticks=1, tick_interval=1, snapshot={"raw_damage_per_tick": 25})
        result = self.attack(battle_id=identity, now=101.1)
        self.assertTrue(any(e["damage_source"] == "test_dot" for e in result["combat_resolution"]["events"]))

    def test_silence_allows_basic_but_blocks_regen_and_new_venom(self):
        self.set_state(enemy_hp=500); self.equip("null_bloom")
        with patch.object(api, "elite_profile", return_value={"elite": True, "elite_modifier": "regenerating", "elite_description": ""}):
            result = self.attack("null_bloom", now=100)
            self.assertEqual(result["elite_healing"], 0)
        self.update(last_enemy_attack_at=0)
        with patch.object(api, "elite_profile", return_value={"elite": True, "elite_modifier": "venomous", "elite_description": ""}):
            attacked = self.enemy_attack(101)
        self.assertTrue(attacked["enemy_attacked"])
        self.assertFalse(any(e["source_id"] == "elite_venomous" for e in attacked["status_effects"]["active"]))

    def test_existing_venom_ticks_while_silenced_and_frenzy_base_remains(self):
        self.set_state(last_enemy_attack_at=0)
        self.status("silence", "null_bloom", 100, 4)
        self.status("damage_over_time", "elite_venomous", 100, 4, target="hero",
                    source_kind="elite", damage_type="poison", max_ticks=3,
                    tick_interval=1, snapshot={"raw_damage_per_tick": 10})
        with patch.object(api, "elite_profile", return_value={"elite": True, "elite_modifier": "frenzy", "elite_description": ""}):
            with patch.object(api.time, "time", return_value=100.5):
                public = api.build_player_response(self.load_player())
            self.assertAlmostEqual(public["enemy_attack_interval"], max(api.MIN_ENEMY_ATTACK_INTERVAL, 1/api.ENEMY_ATTACK_SPEED)/1.20)
            attacked = self.enemy_attack(101.1)
        self.assertTrue(attacked["enemy_attacked"])
        self.assertTrue(any(e["damage_source"] == "elite_venomous" for e in attacked["status_effects"]["tick_events"]))

    def test_timing_slow_expiry_and_root_priority(self):
        self.set_state(last_enemy_attack_at=100)
        with patch.object(api.time, "time", return_value=100):
            normal = api.build_player_response(self.load_player())["enemy_attack_interval"]
        self.status("slow", "test_slow", 100, 3, potency=.25)
        with patch.object(api.time, "time", return_value=100.1):
            slowed = api.build_player_response(self.load_player())["enemy_attack_interval"]
        self.assertAlmostEqual(slowed, normal * 1.25)
        early = self.enemy_attack(100.1)
        self.assertFalse(early["enemy_attacked"]); self.assertEqual(early["hero_hp"], 688)
        self.assertFalse(early.get("counter_triggered", False))
        with patch.object(api.time, "time", return_value=104):
            self.assertAlmostEqual(api.build_player_response(self.load_player())["enemy_attack_interval"], normal)
        self.status("root", "test_root", 104, 3)
        rooted = self.enemy_attack(104.1)
        self.assertTrue(rooted["blocked"])

    def test_thorn_debuff_order_lethal_and_identity_cleanup(self):
        self.set_state(enemy_resistances_json=json.dumps({"physical": 0, "nature": .30, "poison": 0, "arcane": 0}))
        self.equip("thorn_burst")
        result = self.attack("thorn_burst", now=100)
        debuff = next(e for e in result["status_effects"]["active"] if e["effect_type"] == "resistance_debuff")
        self.assertEqual(debuff["potency"], -.08)
        connection = sqlite3.connect(":memory:"); connection.execute("CREATE TABLE players (telegram_id INTEGER, enemy_hp INTEGER)")
        player = {"telegram_id": 1, "enemy_hp": 1000, "enemy_max_hp": 1000,
                  "enemy_resistances_json": json.dumps({"nature": .30}), "battle": "a"}
        event = CombatResolver(connection, player, "a", lambda p: p["battle"], lambda *a: {}).resolve(
            damage_source="test", raw_damage=100, attack_source="skill", crit_metadata={}, boss=False,
            context=CombatContext(1, 1, 1000), damage_type="nature", penetration=.10,
            temporary_resistance_modifier=-.08)
        self.assertAlmostEqual(event.base_resistance, .30)
        self.assertAlmostEqual(event.resistance_before, .22)
        self.assertAlmostEqual(event.resistance_after_penetration, .12)
        connection.close()
        api.STATUS_EFFECTS.clear(); self.set_state(enemy_hp=10); self.equip("thorn_burst")
        lethal = self.attack("thorn_burst", now=200)
        self.assertFalse(any(e["effect_type"] == "resistance_debuff" for e in lethal["status_effects"]["active"]))

    def test_shield_cleanse_priority_nondispellable_and_empty(self):
        self.set_state(); self.equip("mushroom_shield")
        root = self.status("root", "root", 100, 10, target="hero", dispellable=False)
        dot = self.status("damage_over_time", "dot", 100, 10, target="hero")
        silence = self.status("silence", "silence", 100, 10, target="hero")
        with patch.object(api.time, "time", return_value=101):
            cleaned = api.use_mushroom_shield("test")
        self.assertEqual((cleaned["cleansed_effect_id"], cleaned["cleansed_effect_type"]),
                         (silence.effect_id, "silence"))
        active_ids = {e["effect_id"] for e in cleaned["status_effects"]["active"]}
        self.assertIn(root.effect_id, active_ids); self.assertIn(dot.effect_id, active_ids)
        self.assertGreater(cleaned["shield_gained"], 0)
        api.STATUS_EFFECTS.clear(); self.update(mushroom_shield_last_used_at=0)
        with patch.object(api.time, "time", return_value=102):
            empty = api.use_mushroom_shield("test")
        self.assertIsNone(empty["cleansed_effect_id"]); self.assertGreater(empty["shield_gained"], 0)

    def test_elite_generation_and_fortified_weakness(self):
        base = {"telegram_id": 1, "kills_in_stage": 0, "boss_active": 0}
        stage14 = {**base, "stage": 14, "enemy_max_hp": api.calculate_enemy_hp(14)}
        self.assertFalse(api.elite_profile(stage14)["elite"])
        stage15 = {**base, "stage": 15, "enemy_max_hp": api.calculate_enemy_hp(15)}
        self.assertEqual(api.elite_profile(stage15), api.elite_profile(stage15))
        self.assertFalse(api.elite_profile({**stage15, "boss_active": 1})["elite"])
        fortified = api.fortified_resistances({"physical": .15, "nature": .30, "poison": .25, "arcane": .40})
        self.assertLessEqual(min(fortified.values()), .20)

    def test_regeneration_never_runs_after_lethal_action(self):
        self.set_state(enemy_hp=10)
        with patch.object(api, "elite_profile", return_value={"elite": True, "elite_modifier": "regenerating", "elite_description": ""}):
            result = self.attack(now=100)
        self.assertTrue(result["enemy_defeated"]); self.assertEqual(result["elite_healing"], 0)


if __name__ == "__main__":
    unittest.main()
