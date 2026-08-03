import inspect
import unittest

import api
from progression_systems import victory_progression_reward


class PremiumCrystalPolicyTest(unittest.TestCase):
    def test_battle_rewards_never_contain_premium_crystals(self):
        for boss, elite in ((False, False), (False, True), (True, False)):
            reward = victory_progression_reward("identity", 1000, boss=boss, elite=elite)
            self.assertNotIn("premium_crystals", reward)

    def test_repeatable_economy_routes_do_not_write_premium_crystals(self):
        functions = (
            api._resolve_victory, api.salvage_inventory, api.salvage_inventory_bulk,
            api.reroll_inventory_secondary, api.upgrade_chest,
            api._upgrade_progression,
        )
        for function in functions:
            with self.subTest(function=function.__name__):
                self.assertNotIn("premium_crystals", inspect.getsource(function))

    def test_no_premium_quest_route_or_grant_exists_in_this_package(self):
        paths = {route.path for route in api.app.routes}
        self.assertNotIn("/quests/premium-reward", paths)
        self.assertNotIn("/quests/premium-crystals/claim", paths)
        # The pre-existing daily quest claim remains unchanged and is not the
        # future dedicated premium reward transaction.
        self.assertNotIn("premium_crystals", inspect.getsource(api.claim_daily_quest))


if __name__ == "__main__":
    unittest.main()
