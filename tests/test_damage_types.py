import unittest

from damage_types import (
    ARCHETYPE_RESISTANCES, generate_enemy_profile, incoming_damage_breakdown,
    resistance_breakdown,
)


class DamageTypesTest(unittest.TestCase):
    def test_resistance_formula_and_caps(self):
        self.assertEqual(resistance_breakdown(100, "physical", .25)["damage_after_resistance"], 75)
        self.assertEqual(resistance_breakdown(100, "physical", -.20)["damage_after_resistance"], 120)
        self.assertEqual(resistance_breakdown(100, "physical", 2)["damage_after_resistance"], 25)
        self.assertEqual(resistance_breakdown(100, "physical", -2)["damage_after_resistance"], 150)
        self.assertEqual(resistance_breakdown(-100, "physical", .25)["damage_after_resistance"], 0)

    def test_penetration_and_true_damage(self):
        result = resistance_breakdown(100, "nature", .40, .20)
        self.assertEqual(result["resistance_after_penetration"], .20)
        self.assertEqual(result["damage_after_resistance"], 80)
        self.assertEqual(resistance_breakdown(100, "nature", -.40, .50)["resistance_after_penetration"], -.50)
        true = resistance_breakdown(100, "true", .75, .50)
        self.assertEqual(true["damage_after_resistance"], 100)
        self.assertEqual(true["penetration_used"], 0)

    def test_archetypes_and_determinism(self):
        self.assertGreater(ARCHETYPE_RESISTANCES["brute"]["physical"], ARCHETYPE_RESISTANCES["brute"]["arcane"])
        self.assertGreater(ARCHETYPE_RESISTANCES["mystic"]["arcane"], ARCHETYPE_RESISTANCES["mystic"]["physical"])
        self.assertGreater(ARCHETYPE_RESISTANCES["toxic"]["poison"], ARCHETYPE_RESISTANCES["toxic"]["nature"])
        self.assertTrue(all(0 < value < .25 for value in ARCHETYPE_RESISTANCES["guardian"].values()))
        first = generate_enemy_profile(500, True, "same")
        self.assertEqual(first, generate_enemy_profile(500, True, "same"))
        self.assertLess(min(first["resistances"].values()), 0)

    def test_incoming_resistance_precedes_reduction(self):
        result = incoming_damage_breakdown(100, "nature", {"nature": .20}, .50)
        self.assertEqual(result["damage_after_resistance"], 80)
        self.assertEqual(result["damage_after_reduction"], 40)


if __name__ == "__main__":
    unittest.main()
