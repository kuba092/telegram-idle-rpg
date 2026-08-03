import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gear_progression_simulator import (  # noqa: E402
    GearProgressionSimulator,
    Item,
    load_config,
    pity_comparison,
    simulate_all,
)


class GearProgressionSimulatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = ROOT / "tools" / "gear_progression_config.json"
        cls.config = load_config(cls.config_path)

    def test_config_is_json_and_contains_all_profiles(self):
        parsed = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(set(parsed["profiles"]), {"f2p", "low_spender", "mid_spender", "whale"})
        self.assertEqual(parsed["simulation"]["checkpoints"], [1, 3, 7, 14, 30, 60, 90, 180, 300, 365])

    def test_skill_growth_is_bounded_and_uses_level_minus_one(self):
        growth = self.config["skills"]["power_per_level"]
        self.assertLessEqual(growth, 0.08)
        self.assertEqual(1 + growth * (1 - 1), 1.0)
        self.assertAlmostEqual(1 + growth * (20 - 1), 1.95)

    def test_item_contains_every_required_dimension(self):
        simulator = GearProgressionSimulator(self.config, "f2p", seed=1)
        simulator.state.max_stage = 25
        simulator.state.hero_level = 7
        simulator.state.chest_level = 4
        item = simulator.generate_item()
        self.assertIn(item.slot, self.config["item"]["slots"])
        self.assertGreaterEqual(item.power, 1)
        self.assertGreaterEqual(item.damage, 0)
        self.assertGreaterEqual(item.hp, 0)
        self.assertEqual(item.hero_level, 7)
        self.assertLessEqual(item.stage, 7 * self.config["hero"]["equipment_stage_cap_per_level"])
        self.assertEqual(item.chest_level, 4)

    def test_same_seed_is_deterministic(self):
        first = GearProgressionSimulator(self.config, "f2p", seed=77).run(14)
        second = GearProgressionSimulator(self.config, "f2p", seed=77).run(14)
        self.assertEqual(first["snapshots"], second["snapshots"])

    def test_profiles_are_bounded_and_ordered_by_resources(self):
        results = simulate_all(self.config, days=30)
        opened = [results[name]["snapshots"][-1]["opened_chests"] for name in self.config["profiles"]]
        self.assertEqual(opened, sorted(opened))
        self.assertLess(opened[-1], 100_000)

    def test_pity_forces_upgrade_after_configured_streak(self):
        config = copy.deepcopy(self.config)
        config["pity"]["guaranteed_upgrade_after"] = 2
        simulator = GearProgressionSimulator(config, "f2p", pity=True, seed=3)
        simulator.state.max_stage = 1
        simulator.state.equipment = {
            slot: simulator.generate_item()
            for slot in config["item"]["slots"]
        }
        simulator.state.no_upgrade_streak = 1
        weakest_before = min(item.power for item in simulator.state.equipment.values())
        item = simulator.generate_item()
        self.assertGreater(item.power, weakest_before)

    def test_probability_fields_are_valid(self):
        result = GearProgressionSimulator(self.config, "f2p", seed=9).run(3)["snapshots"][-1]
        for key in ("no_upgrade_probability_50", "no_upgrade_probability_100", "no_upgrade_probability_500"):
            self.assertGreaterEqual(result[key], 0)
            self.assertLessEqual(result[key], 1)
        self.assertGreaterEqual(result["no_upgrade_probability_50"], result["no_upgrade_probability_100"])
        self.assertGreaterEqual(result["no_upgrade_probability_100"], result["no_upgrade_probability_500"])

    def test_f2p_has_progress_and_no_permanent_year_wall(self):
        final = GearProgressionSimulator(self.config, "f2p", seed=11).run(365)["snapshots"][-1]
        self.assertGreater(final["max_stage"], 100)
        self.assertLess(final["longest_soft_wall_days"], 120)

    def test_pity_comparison_has_both_variants(self):
        result = pity_comparison(self.config, days=7)
        self.assertIn("with_pity", result["f2p"])
        self.assertIn("without_pity", result["f2p"])

    def combat_simulator(self, *, hero_level=50, day=365, profile="f2p"):
        simulator = GearProgressionSimulator(copy.deepcopy(self.config), profile, seed=123)
        simulator.state.day = day
        simulator.state.hero_level = hero_level
        simulator.state.max_stage = 500
        equipment = {}
        for index, (slot, focus) in enumerate(simulator.config["item"]["slots"].items()):
            power = 1000 + index * 10
            damage = 500 if focus == "attack" else 250 if focus == "mixed" else 0
            hp = 3800 if focus == "defense" else 1900 if focus == "mixed" else 0
            equipment[slot] = Item(slot, "epic", 4, power, damage, hp, hero_level, 400, 20, 1.0, 100)
        simulator.state.equipment = equipment
        return simulator

    def add_build(self, simulator, name, skills, companions):
        simulator.config["builds"][name] = {
            "skills": skills,
            "companions": companions,
        }

    def test_locked_skill_slots_do_not_apply(self):
        simulator = self.combat_simulator(hero_level=10)
        active = simulator.active_build("balanced")
        self.assertEqual([entry["id"] for entry in active["skills"]], ["spore_strike"])

    def test_locked_companion_slots_do_not_apply(self):
        simulator = self.combat_simulator(hero_level=10)
        active = simulator.active_build("balanced")
        self.assertEqual([entry["id"] for entry in active["companions"]], ["forest_sprite"])
        simulator.state.hero_level = 9
        self.assertEqual(simulator.active_build("balanced")["companions"], [])

    def test_forest_sprite_increases_attacks_and_skills(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain", ["spore_strike"], [])
        self.add_build(simulator, "forest", ["spore_strike"], ["forest_sprite"])
        plain = simulator.simulate_battle(500, "plain", boss=True)
        forest = simulator.simulate_battle(500, "forest", boss=True)
        self.assertGreater(forest["normal_attack_damage"], plain["normal_attack_damage"])
        self.assertGreater(forest["damage_by_source"]["spore_strike"], plain["damage_by_source"]["spore_strike"])

    def test_forest_full_bonus_for_attack_and_spore_half_for_poison(self):
        simulator = self.combat_simulator(profile="whale")
        simulator.profile["companion_level_day_365"] = 20
        self.add_build(simulator, "plain_all", ["spore_strike", "poison_cloud"], [])
        self.add_build(simulator, "forest_all", ["spore_strike", "poison_cloud"], ["forest_sprite"])
        plain = simulator.simulate_battle(1, "plain_all", target_duration=5)
        forest = simulator.simulate_battle(1, "forest_all", target_duration=5)
        self.assertAlmostEqual(forest["normal_attack_damage"] / plain["normal_attack_damage"], 1.26)
        self.assertAlmostEqual(forest["damage_by_source"]["spore_strike"] / plain["damage_by_source"]["spore_strike"], 1.26)
        self.assertAlmostEqual(forest["damage_by_source"]["poison_cloud"] / plain["damage_by_source"]["poison_cloud"], 1.13)

    def test_spore_beetle_increases_only_normal_attack(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain", ["spore_strike"], [])
        self.add_build(simulator, "beetle", ["spore_strike"], ["spore_beetle"])
        plain = simulator.simulate_battle(500, "plain", boss=True)
        beetle = simulator.simulate_battle(500, "beetle", boss=True)
        self.assertGreater(beetle["normal_attack_damage"], plain["normal_attack_damage"])
        # Compare the first cast rather than total battle damage (battle lengths differ).
        level = beetle["active_skills"]["spore_strike"]
        expected_cast = simulator.combat_stats(build_name="beetle")["raw_damage"] * 2 * (1 + (level - 1) * simulator.config["skills"]["power_per_level"])
        self.assertAlmostEqual(beetle["damage_by_source"]["spore_strike"] / beetle["skill_uses"]["spore_strike"], expected_cast * beetle["expected_crit_multiplier"], places=5)
        self.assertAlmostEqual(
            beetle["expected_critical_attack_damage"],
            beetle["normal_attack_damage"] * beetle["crit_damage_multiplier"],
        )

    def test_spore_beetle_speeds_attacks_but_not_skills(self):
        simulator = self.combat_simulator(profile="whale")
        simulator.profile["companion_level_day_365"] = 20
        self.add_build(simulator, "plain_speed", ["spore_strike"], [])
        self.add_build(simulator, "beetle_speed", ["spore_strike"], ["spore_beetle"])
        plain = simulator.simulate_battle(1, "plain_speed", target_duration=20)
        beetle = simulator.simulate_battle(1, "beetle_speed", target_duration=20)
        self.assertAlmostEqual(beetle["attack_interval"], plain["attack_interval"] / 1.13)
        self.assertEqual(beetle["skill_intervals"], plain["skill_intervals"])

    def test_mushroom_owl_reduces_real_skill_intervals_and_caps_at_30_percent(self):
        simulator = self.combat_simulator(profile="whale")
        self.add_build(simulator, "owl", ["spore_strike", "poison_cloud"], ["mushroom_owl"])
        battle = simulator.simulate_battle(700, "owl", boss=True)
        owl_level = battle["active_companions"]["mushroom_owl"]
        expected_reduction = min(0.30, owl_level * 0.015)
        self.assertAlmostEqual(battle["skill_intervals"]["spore_strike"], 8 * (1 - expected_reduction))
        simulator.profile["companion_level_day_365"] = 20
        capped = simulator.simulate_battle(700, "owl", boss=True)
        self.assertAlmostEqual(capped["skill_intervals"]["spore_strike"], 5.6)

    def test_owl_buffs_repeats_not_first_use_and_resets_each_battle(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "owl_repeat", ["spore_strike"], ["mushroom_owl"])
        first = simulator.simulate_battle(1, "owl_repeat", target_duration=20)
        second = simulator.simulate_battle(1, "owl_repeat", target_duration=20)
        multipliers = first["skill_cast_multipliers"]["spore_strike"]
        self.assertEqual(multipliers[0], 1.0)
        self.assertGreater(multipliers[1], multipliers[0])
        self.assertEqual(first["skill_cast_multipliers"], second["skill_cast_multipliers"])

    def test_thorn_wolf_increases_expected_critical_dps(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain", ["spore_strike"], [])
        self.add_build(simulator, "wolf", ["spore_strike"], ["thorn_wolf"])
        plain = simulator.simulate_battle(700, "plain", boss=True)
        wolf = simulator.simulate_battle(700, "wolf", boss=True)
        self.assertGreater(wolf["expected_crit_multiplier"], plain["expected_crit_multiplier"])
        self.assertGreater(wolf["crit_chance"], plain["crit_chance"])
        self.assertGreater(wolf["crit_damage_multiplier"], plain["crit_damage_multiplier"])
        self.assertGreater(wolf["total_dps"], plain["total_dps"])

    def test_level_20_companion_caps_and_values(self):
        simulator = self.combat_simulator(profile="whale")
        simulator.profile["companion_level_day_365"] = 20
        self.add_build(simulator, "forest20", [], ["forest_sprite"])
        self.add_build(simulator, "slime20", [], ["baby_slime"])
        self.add_build(simulator, "owl20", ["spore_strike"], ["mushroom_owl"])
        raw = simulator.combat_stats(build_name="forest20")["raw_damage"]
        self.assertAlmostEqual(simulator.combat_stats(build_name="forest20")["damage"], raw * 1.26)
        raw_hp = simulator.combat_stats(build_name="slime20")["raw_hp"]
        self.assertAlmostEqual(simulator.combat_stats(build_name="slime20")["hp"], raw_hp * 1.3)
        owl = simulator.simulate_battle(700, "owl20", boss=True)
        self.assertAlmostEqual(owl["skill_intervals"]["spore_strike"], 5.6)

    def test_baby_slime_increases_hp(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain", [], [])
        self.add_build(simulator, "slime", [], ["baby_slime"])
        self.assertGreater(
            simulator.combat_stats(build_name="slime")["hp"],
            simulator.combat_stats(build_name="plain")["hp"],
        )

    def test_mushroom_shield_increases_effective_hp(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain", [], [])
        self.add_build(simulator, "shield", ["mushroom_shield"], [])
        plain = simulator.simulate_battle(1000, "plain", boss=True)
        shield = simulator.simulate_battle(1000, "shield", boss=True)
        self.assertGreater(shield["effective_hp"], plain["effective_hp"])
        expected_shield = shield["max_hp"] * 0.30 * (1 + (shield["active_skills"]["mushroom_shield"] - 1) * simulator.config["skills"]["power_per_level"])
        self.assertAlmostEqual(shield["shield_generated"], expected_shield * shield["skill_uses"]["mushroom_shield"])

    def test_broken_shield_reduces_damage_without_stacking(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "shield_break", ["mushroom_shield"], [])
        battle = simulator.simulate_battle(1000, "shield_break", boss=True)
        self.assertGreater(battle["shield_breaks"], 0)
        self.assertGreater(battle["shield_damage_reduced"], 0)
        configured = simulator.config["skills"]["mushroom_shield"]["break_damage_reduction"]
        self.assertEqual(configured, 0.10)

    def test_shield_auto_cast_threshold_is_60_percent(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "shield_threshold", ["mushroom_shield"], [])
        max_hp = simulator.combat_stats(build_name="shield_threshold")["hp"]
        at_threshold = simulator.simulate_battle(
            1000, "shield_threshold", boss=True, initial_hp=max_hp * 0.60
        )
        above_threshold = simulator.simulate_battle(
            1000, "shield_threshold", boss=True, initial_hp=max_hp * 0.61
        )
        self.assertEqual(at_threshold["shield_cast_times"][0], 2.0)
        self.assertGreater(above_threshold["shield_cast_times"][0], 2.0)

    def test_ancient_entling_heals_only_after_victory(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "ent", [], ["ancient_entling"])
        battle = simulator.simulate_battle(400, "ent", boss=False)
        self.assertTrue(battle["won"])
        self.assertGreater(battle["post_victory_healing"], 0)
        self.assertLess(battle["hero_hp_after_battle"], battle["max_hp"])
        self.assertLessEqual(battle["post_victory_healing"], battle["max_hp"] - battle["hero_hp_after_battle"])
        losing = simulator.simulate_battle(1000, "ent", boss=True)
        if not losing["won"]:
            self.assertEqual(losing["post_victory_healing"], 0)

    def test_level_20_entling_heals_12_percent_normal_and_24_percent_boss(self):
        simulator = self.combat_simulator(profile="whale")
        simulator.profile["companion_level_day_365"] = 20
        self.add_build(simulator, "ent20", [], ["ancient_entling"])
        normal = simulator.simulate_battle(300, "ent20", boss=False)
        boss = simulator.simulate_battle(200, "ent20", boss=True)
        self.assertTrue(normal["won"] and boss["won"])
        self.assertAlmostEqual(normal["post_victory_healing"], normal["max_hp"] * 0.12)
        self.assertAlmostEqual(boss["post_victory_healing"], boss["max_hp"] * 0.24)

    def test_poison_cloud_does_not_land_all_ticks_in_short_fight(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "poison", ["poison_cloud"], [])
        battle = simulator.simulate_battle(1, "poison", boss=False)
        self.assertLess(battle["poison_ticks_landed"], 5)

    def test_poison_cloud_uses_45_percent_base_damage_per_tick(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "poison", ["poison_cloud"], [])
        battle = simulator.simulate_battle(500, "poison", boss=True, target_duration=5)
        level = battle["active_skills"]["poison_cloud"]
        per_tick = simulator.combat_stats(build_name="poison")["raw_damage"] * 0.45 * (1 + (level - 1) * simulator.config["skills"]["power_per_level"])
        self.assertAlmostEqual(
            battle["damage_by_source"]["poison_cloud"],
            battle["poison_ticks_landed"] * per_tick * battle["expected_crit_multiplier"],
            places=4,
        )

    def test_poison_ramps_only_after_a_complete_cloud(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "poison_ramp", ["poison_cloud"], [])
        short = simulator.simulate_battle(1, "poison_ramp", target_duration=4)
        long = simulator.simulate_battle(1, "poison_ramp", target_duration=50)
        self.assertEqual(short["poison_completed_stacks"], 0)
        self.assertEqual(short["poison_cloud_cast_multipliers"], [1.0])
        self.assertGreater(long["poison_cloud_cast_multipliers"][1], 1.0)
        self.assertLessEqual(long["poison_completed_stacks"], 3)

    def test_full_stage_carries_hp_and_entling_heals_between_fights(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain_sequence", [], [])
        self.add_build(simulator, "ent_sequence", [], ["ancient_entling"])
        plain = simulator.simulate_full_stage(300, "plain_sequence")
        entling = simulator.simulate_full_stage(300, "ent_sequence")
        self.assertLess(plain["battle_start_hp"][1], plain["battle_start_hp"][0])
        self.assertGreater(entling["entling_healing"], 0)
        self.assertGreaterEqual(entling["hp_before_boss"], plain["hp_before_boss"])
        self.assertTrue(entling["cooldowns_reset_between_fights"])

    def test_entling_cannot_save_single_boss_and_slime_does_not_restore_missing_hp(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "ent_single", [], ["ancient_entling"])
        self.add_build(simulator, "slime_missing", [], ["baby_slime"])
        losing = simulator.simulate_battle(1000, "ent_single", boss=True)
        self.assertFalse(losing["won"])
        self.assertEqual(losing["post_victory_healing"], 0)
        missing = simulator.simulate_battle(1, "slime_missing", initial_hp=1000, target_duration=1)
        self.assertEqual(missing["hero_hp_at_start"], 1000)

    def test_tank_single_boss_and_sustain_long_sequence_roles(self):
        simulator = self.combat_simulator(profile="mid_spender")
        tank = simulator.simulate_battle(1000, "tank", boss=True)
        basic = simulator.simulate_battle(1000, "basic_attack", boss=True)
        self.assertGreater(tank["shield_absorbed"] + tank["shield_damage_reduced"], basic["shield_absorbed"])
        sustain = simulator.simulate_full_stage(400, "sustain")
        basic_stage = simulator.simulate_full_stage(400, "basic_attack")
        self.assertGreater(sustain["remaining_hp_after_boss_percent"], basic_stage["remaining_hp_after_boss_percent"])

    def test_defensive_score_does_not_use_effective_hp(self):
        simulator = self.combat_simulator()
        comparison = simulator.compare_builds(500, monte_carlo=False)
        self.assertNotIn("effective_hp", comparison["defensive_score_inputs"])

    def test_basic_attack_is_within_target_range_at_20_seconds(self):
        simulator = GearProgressionSimulator(copy.deepcopy(self.config), "f2p", seed=123)
        simulator.run(365)
        basic = simulator.damage_over_duration("basic_attack", 20)["dps"]
        burst = simulator.damage_over_duration("burst", 20)["dps"]
        deficit = 1 - basic / burst
        self.assertGreaterEqual(deficit, 0.10)
        self.assertLessEqual(deficit, 0.18)

    def test_burst_wins_5_seconds_and_skill_spam_wins_180_seconds(self):
        simulator = self.combat_simulator(profile="mid_spender")
        self.assertGreater(simulator.damage_over_duration("burst", 5)["dps"], simulator.damage_over_duration("skill_spam", 5)["dps"])
        self.assertGreater(simulator.damage_over_duration("skill_spam", 180)["dps"], simulator.damage_over_duration("burst", 180)["dps"])

    def test_burst_and_skill_spam_are_not_identical(self):
        self.assertNotEqual(self.config["builds"]["burst"], self.config["builds"]["skill_spam"])

    def test_late_f2p_progression_ranges_and_no_early_stage_1000(self):
        ranges = {
            "basic_f2p": (850, 950), "burst": (950, 1000),
            "basic_attack": (900, 975), "skill_spam": (950, 1000),
            "tank": (825, 950), "sustain": (850, 975),
            "balanced": (925, 1000),
        }
        for build_name, (minimum, maximum) in ranges.items():
            with self.subTest(build=build_name):
                config = copy.deepcopy(self.config)
                config["build_simulation"]["default_progression_build"] = build_name
                result = GearProgressionSimulator(config, "f2p", seed=20260803).run(365)
                snapshots = {row["day"]: row for row in result["snapshots"]}
                self.assertLess(snapshots[300]["max_stage"], 1000)
                self.assertGreaterEqual(snapshots[365]["max_stage"], minimum)
                self.assertLessEqual(snapshots[365]["max_stage"], maximum)

    def test_builds_differ_and_tank_outlives_burst_while_burst_has_more_dps(self):
        simulator = self.combat_simulator(profile="mid_spender")
        burst = simulator.simulate_battle(700, "burst", boss=True)
        tank = simulator.simulate_battle(700, "tank", boss=True)
        self.assertGreater(tank["effective_hp"], burst["effective_hp"])
        self.assertGreater(burst["total_dps"], tank["total_dps"])
        self.assertNotEqual(burst["skill_uses"], tank["skill_uses"])

    def test_combat_monte_carlo_same_seed_is_deterministic(self):
        first = self.combat_simulator().monte_carlo_battle(500, "balanced", boss=True, trials=20)
        second = self.combat_simulator().monte_carlo_battle(500, "balanced", boss=True, trials=20)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
