import unittest

from summon_system import (BANNERS, COSTS, RATES, advance, choose_entity, guarantee,
                           normalize_fragments, normalize_state, roll_rarity)


CATALOG = {
    "c": {"rarity": "common"}, "r": {"rarity": "rare"},
    "e": {"rarity": "epic"}, "l": {"rarity": "legendary"},
}


class SummonSystemTest(unittest.TestCase):
    def test_rates_sum_to_one_hundred_and_banners_exist(self):
        self.assertEqual(sum(RATES.values()), 100.0)
        self.assertEqual(set(BANNERS), {"skill_standard", "companion_standard"})

    def test_costs_are_explicit(self):
        self.assertEqual(COSTS[1]["premium_crystals"], 100)
        self.assertEqual(COSTS[10]["premium_crystals"], 900)

    def test_seed_and_state_are_deterministic(self):
        first = [(roll_rarity("same", i), choose_entity(CATALOG, roll_rarity("same", i), "same", i)) for i in range(10)]
        second = [(roll_rarity("same", i), choose_entity(CATALOG, roll_rarity("same", i), "same", i)) for i in range(10)]
        self.assertEqual(first, second)

    def test_empty_uncommon_falls_down_without_unknown_id(self):
        entity, rarity = choose_entity(CATALOG, "uncommon", "seed", 1)
        self.assertEqual((entity, rarity), ("c", "common"))

    def test_guarantee_priority(self):
        state = normalize_state({"pulls_since_rare": 9, "pulls_since_epic": 49, "pulls_since_legendary": 149})
        self.assertEqual(guarantee(state), "legendary")
        state["pulls_since_legendary"] = 0
        self.assertEqual(guarantee(state), "epic")
        state["pulls_since_epic"] = 0
        self.assertEqual(guarantee(state), "rare")

    def test_pity_resets_by_actual_rarity(self):
        state = normalize_state({"pulls_since_rare": 9, "pulls_since_epic": 12, "pulls_since_legendary": 20})
        advance(state, "epic")
        self.assertEqual(state["pulls_since_rare"], 0)
        self.assertEqual(state["pulls_since_epic"], 0)
        self.assertEqual(state["pulls_since_legendary"], 21)
        advance(state, "legendary")
        self.assertEqual(state["pulls_since_legendary"], 0)

    def test_history_and_fragment_json_are_normalized(self):
        state = normalize_state({"summon_history": list(range(130))})
        self.assertEqual(len(state["summon_history"]), 100)
        self.assertEqual(normalize_fragments('{"c": 5, "bad": 99, "r": -2}', set(CATALOG)), {"c": 5})


if __name__ == "__main__": unittest.main()
