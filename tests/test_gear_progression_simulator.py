import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from gear_progression_simulator import (  # noqa: E402
    GearProgressionSimulator,
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


if __name__ == "__main__":
    unittest.main()
