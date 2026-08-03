import unittest

from offline_progression import OFFLINE_CAP_SECONDS, elapsed, make_snapshot, public_status, rewards


class OfflineProgressionTest(unittest.TestCase):
    def test_less_than_five_minutes_is_zero(self):
        self.assertEqual(elapsed(1000, 1299)["offline_seconds_rewarded"], 0)

    def test_exact_threshold_and_cap(self):
        self.assertEqual(elapsed(1000, 1300)["offline_seconds_rewarded"], 300)
        timing = elapsed(1000, 1000 + OFFLINE_CAP_SECONDS + 999)
        self.assertEqual(timing["offline_seconds_rewarded"], OFFLINE_CAP_SECONDS)
        self.assertTrue(timing["cap_reached"])

    def test_snapshot_uses_server_time_and_player_progress(self):
        snap = make_snapshot({"last_active_at": 100, "stage": 27, "chest_level": 4}, 1000)
        self.assertEqual((snap["ended_at"], snap["stage"], snap["chest_level"]), (1000, 27, 4))

    def test_rewards_are_deterministic_and_stage_changes_gold(self):
        low = rewards(1, 1, 28800, seed="x")
        self.assertEqual(low, rewards(1, 1, 28800, seed="x"))
        self.assertGreater(rewards(100, 1, 28800, seed="x")["gold"], low["gold"])

    def test_all_caps_and_no_premium_or_summon_currency(self):
        result = rewards(999, 99, 999999, seed="cap")
        self.assertEqual(result["reward_units"], 480)
        self.assertLessEqual(result["chest_xp"], 48)
        self.assertLessEqual(result["skill_tomes"], 6)
        self.assertLessEqual(result["companion_essence"], 6)
        self.assertLessEqual(result["refinement_ore"], 2)
        self.assertEqual(result["premium_crystals"], 0)
        self.assertNotIn("skill_summon_scrolls", result)
        self.assertNotIn("companion_summon_contracts", result)

    def test_public_contract(self):
        block = public_status({"last_active_at": 100, "stage": 1, "chest_level": 1,
                               "offline_claim_version": 3, "offline_unclaimed_json": "{}"}, 500)
        self.assertEqual(block["server_time"], 500)
        self.assertEqual(block["claim_version"], 3)
        self.assertTrue(block["claimable"])


if __name__ == "__main__": unittest.main()
