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
        self.assertEqual(parsed["simulation"]["checkpoints"], [1, 3, 7, 14, 30, 60, 90, 180, 365])

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

    def test_spore_beetle_increases_only_normal_attack(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain", ["spore_strike"], [])
        self.add_build(simulator, "beetle", ["spore_strike"], ["spore_beetle"])
        plain = simulator.simulate_battle(500, "plain", boss=True)
        beetle = simulator.simulate_battle(500, "beetle", boss=True)
        self.assertGreater(beetle["normal_attack_damage"], plain["normal_attack_damage"])
        # Compare the first cast rather than total battle damage (battle lengths differ).
        level = beetle["active_skills"]["spore_strike"]
        expected_cast = simulator.combat_stats(build_name="beetle")["raw_damage"] * 2 * (1 + (level - 1) * 0.05)
        self.assertAlmostEqual(beetle["damage_by_source"]["spore_strike"] / beetle["skill_uses"]["spore_strike"], expected_cast * beetle["expected_crit_multiplier"], places=5)

    def test_mushroom_owl_reduces_real_skill_intervals_and_caps_at_30_percent(self):
        simulator = self.combat_simulator(profile="whale")
        self.add_build(simulator, "owl", ["spore_strike", "poison_cloud"], ["mushroom_owl"])
        battle = simulator.simulate_battle(700, "owl", boss=True)
        owl_level = battle["active_companions"]["mushroom_owl"]
        expected_reduction = min(0.30, owl_level * 0.02)
        self.assertAlmostEqual(battle["skill_intervals"]["spore_strike"], 8 * (1 - expected_reduction))
        simulator.profile["companion_level_day_365"] = 20
        capped = simulator.simulate_battle(700, "owl", boss=True)
        self.assertAlmostEqual(capped["skill_intervals"]["spore_strike"], 5.6)

    def test_thorn_wolf_increases_expected_critical_dps(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "plain", ["spore_strike"], [])
        self.add_build(simulator, "wolf", ["spore_strike"], ["thorn_wolf"])
        plain = simulator.simulate_battle(700, "plain", boss=True)
        wolf = simulator.simulate_battle(700, "wolf", boss=True)
        self.assertGreater(wolf["expected_crit_multiplier"], plain["expected_crit_multiplier"])
        self.assertGreater(wolf["total_dps"], plain["total_dps"])

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

    def test_poison_cloud_does_not_land_all_ticks_in_short_fight(self):
        simulator = self.combat_simulator()
        self.add_build(simulator, "poison", ["poison_cloud"], [])
        battle = simulator.simulate_battle(1, "poison", boss=False)
        self.assertLess(battle["poison_ticks_landed"], 5)

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
