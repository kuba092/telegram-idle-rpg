import unittest

from awakening_system import (
    advance_awakening, advance_star, awakening_cost, awakening_nodes,
    normalize_rank_state, public_rank, rank_multiplier, star_cost,
)


class AwakeningSystemTest(unittest.TestCase):
    def test_old_and_corrupt_state_is_normalized(self):
        self.assertEqual(normalize_rank_state({})["rank"], 0)
        state = normalize_rank_state({"rank": 99, "rank_stars": -8, "awakening_tier": 99,
                                      "rank_version": "bad", "awakening_nodes": "bad"})
        self.assertEqual((state["rank"], state["rank_stars"], state["awakening_tier"]), (5, 0, 3))
        self.assertEqual(state["rank_version"], 1)

    def test_costs_and_growth(self):
        expected = {"common": 5, "uncommon": 8, "rare": 12, "epic": 20, "legendary": 35}
        for rarity, cost in expected.items(): self.assertEqual(star_cost(rarity, 0), cost)
        self.assertGreater(star_cost("common", 12), star_cost("common", 1))
        self.assertEqual(awakening_cost("epic", 0), 110)
        self.assertEqual(awakening_cost("legendary", 2), 375)
        self.assertIsNone(star_cost("common", 25))

    def test_rank_transition_and_multiplier_do_not_double_count(self):
        state = {"rank": 0, "rank_stars": 4}
        state, advanced = advance_star(state)
        self.assertTrue(advanced)
        self.assertEqual((state["rank"], state["rank_stars"]), (1, 0))
        self.assertAlmostEqual(rank_multiplier(state), 1 + 5 * .012 + .02)

    def test_awakening_and_nodes_are_linear(self):
        state = {"rank": 5, "rank_stars": 0, "awakening_tier": 0}
        for tier in range(1, 4):
            state = advance_awakening(state)
            self.assertEqual(state["awakening_tier"], tier)
        nodes = awakening_nodes("companion", 2)
        self.assertEqual([n["unlocked"] for n in nodes], [True, True, False])

    def test_public_availability_is_server_owned(self):
        block = public_rank({"rank": 0}, 5, "common", "skill", {"damage": 12})
        self.assertTrue(block["rank_up_available"])
        self.assertEqual(block["effective_preview"], {"damage": 12})
        self.assertFalse(block["awakening_available"])


if __name__ == "__main__":
    unittest.main()
