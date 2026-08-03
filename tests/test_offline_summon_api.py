import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import api
import quest_system as quests
from offline_progression import make_snapshot
from summon_system import normalize_state


PLAYER_ID = 88001


class OfflineSummonApiIntegrationTest(unittest.TestCase):
    """Real route functions and SQLite transactions on an isolated database."""

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_patch = patch.object(api, "DATABASE_PATH", str(Path(self.temp_directory.name) / "game.db"))
        self.auth_patch = patch.object(api, "validate_telegram_data", return_value={"id": PLAYER_ID, "first_name": "Test"})
        self.database_patch.start(); self.auth_patch.start(); api.ACTION_CACHE.clear()
        api.create_database()
        api.get_or_create_player({"id": PLAYER_ID, "first_name": "Test"})
        self._install_future_quest_objectives()

    def tearDown(self):
        api.ACTION_CACHE.clear(); self.auth_patch.stop(); self.database_patch.stop(); self.temp_directory.cleanup()

    def db(self):
        return api.get_database()

    def load(self, *_args, **_kwargs):
        connection = self.db()
        try: return api.load_player(connection, PLAYER_ID)
        finally: connection.close()

    def update(self, **values):
        connection = self.db()
        try:
            connection.execute("UPDATE players SET " + ",".join(f"{key}=?" for key in values) + " WHERE telegram_id=?",
                               (*values.values(), PLAYER_ID))
            connection.commit()
        finally: connection.close()

    def _install_future_quest_objectives(self):
        now = quests.utc_now(); daily_reset, _ = quests.reset_times(now)
        rows = []
        for index, objective in enumerate(("perform_skill_summons", "perform_companion_summons", "claim_offline_rewards")):
            rows.append({"quest_id": f"future-{index}", "objective_type": objective, "target_value": 999,
                         "current_value": 0, "completed": False, "claimed": False, "reward": {},
                         "quest_type": "daily", "reset_at": daily_reset.isoformat(), "version": 1})
        self.update(quest_daily_reset_key=quests.daily_key(now),
                    quest_weekly_reset_key=quests.weekly_key(now),
                    daily_quests_json=json.dumps({"quests": rows, "claimed_milestones": []}))

    def quest_value(self, objective):
        state = json.loads(self.load()["daily_quests_json"])
        return next(int(row["current_value"]) for row in state["quests"] if row["objective_type"] == objective)

    def offline_snapshot(self):
        player = self.load()
        keys = ("gold", "salvage_dust", "chest_xp", "skill_tomes", "companion_essence",
                "refinement_ore", "premium_crystals", "offline_unclaimed_json",
                "offline_last_claim_at", "last_active_at", "offline_claim_version", "daily_quests_json")
        return {key: player[key] for key in keys}

    def summon_snapshot(self, skill=True):
        player = self.load(); prefix = "skill" if skill else "companion"
        return {"tickets": player["skill_summon_scrolls" if skill else "companion_summon_contracts"],
                "crystals": player["premium_crystals"], "state": player[f"{prefix}_summon_state_json"],
                "collection": player["skills_collection_json" if skill else "companions_collection_json"],
                "fragments": player[f"{prefix}_fragments_json"], "quests": player["daily_quests_json"]}

    def seed_offline(self, seconds=600, crystals=77):
        now = int(time.time()); player = self.load(); player["last_active_at"] = now - seconds
        snapshot = make_snapshot(player, now)
        self.update(last_active_at=now, offline_unclaimed_json=json.dumps(snapshot), premium_crystals=crystals)
        return snapshot

    def call_offline(self, action="offline-1", version=None):
        if version is None: version = int(self.load()["offline_claim_version"])
        return api.claim_offline({"client_action_id": action, "expected_claim_version": version}, "test")

    def force_pull(self, entity_id, rarity):
        return patch.multiple(api, roll_rarity=lambda seed, index, minimum=None: minimum or rarity,
                              choose_entity=lambda catalog, rolled, seed, index: (entity_id, api.SKILL_CATALOG.get(entity_id, api.COMPANION_CATALOG.get(entity_id))["rarity"]))

    def call_skill(self, count=1, payment="ticket", action="skill-1", version=None):
        if version is None: version = normalize_state(self.load()["skill_summon_state_json"])["pity_version"]
        return api.summon_skill({"count": count, "payment_type": payment, "client_action_id": action,
                                 "expected_pity_version": version}, "test")

    # Offline routes and durability.
    def test_status_and_repeated_player_do_not_credit_resources(self):
        self.seed_offline(); before = self.offline_snapshot()
        api.offline_status("test"); api.player("test"); api.player("test")
        self.assertEqual(self.offline_snapshot(), before)

    def test_claim_after_five_minutes_duplicate_stale_parallel_and_premium_policy(self):
        snapshot = self.seed_offline(300); version = int(self.load()["offline_claim_version"])
        first = self.call_offline("same", version); after = self.offline_snapshot()
        duplicate = self.call_offline("same", version)
        stale = self.call_offline("stale", version)
        logical_second = self.call_offline("parallel", version)
        self.assertEqual(first["offline_seconds_rewarded"], snapshot["rewarded_seconds"])
        self.assertTrue(duplicate["duplicate_request"]); self.assertEqual(duplicate["rewards"], first["rewards"])
        self.assertTrue(stale["stale_version"]); self.assertTrue(logical_second["stale_version"])
        self.assertEqual(self.offline_snapshot(), after); self.assertEqual(after["premium_crystals"], 77)
        self.assertEqual(self.quest_value("claim_offline_rewards"), 1)

    def test_offline_rollback_restores_everything_and_restart_is_durable(self):
        self.seed_offline(); before = self.offline_snapshot(); original = api.QuestProgressTracker.dispatch
        def fail_after_dispatch(tracker, *args, **kwargs):
            original(tracker, *args, **kwargs); raise RuntimeError("offline rollback")
        with patch.object(api, "get_or_create_player", side_effect=self.load), patch.object(api.QuestProgressTracker, "dispatch", fail_after_dispatch):
            with self.assertRaisesRegex(RuntimeError, "offline rollback"): self.call_offline("rollback")
        self.assertEqual(self.offline_snapshot(), before)
        result = self.call_offline("restart-safe"); api.ACTION_CACHE.clear()
        self.assertEqual(self.call_offline("restart-safe", result["claim_version"] - 1)["rewards"], result["rewards"])

    def test_zero_unit_claim_does_not_increment_quest(self):
        self.update(offline_unclaimed_json="{}")
        self.call_offline("empty")
        self.assertEqual(self.quest_value("claim_offline_rewards"), 0)

    def test_legacy_pool_is_compatible_and_cannot_claim_new_snapshot(self):
        self.seed_offline(); before = self.offline_snapshot()
        legacy = api.claim_offline_reward("test")
        self.assertFalse(legacy["claimed"]); self.assertEqual(self.offline_snapshot(), before)
        self.assertTrue(self.call_offline("new-route")["transaction_completed"])
        self.update(offline_pending_chests=2, offline_pending_exp=0, offline_unclaimed_json="{}")
        legacy_first = api.claim_offline_reward("test"); legacy_second = api.claim_offline_reward("test")
        self.assertTrue(legacy_first["claimed"]); self.assertFalse(legacy_second["claimed"])

    # Currency, idempotency, unlock and duplicate behavior.
    def test_ticket_x1_and_x10_and_quest_actual_pull_count(self):
        self.update(skill_summon_scrolls=11)
        with self.force_pull("spore_strike", "common"):
            one = self.call_skill(action="ticket-1")
            ten = self.call_skill(10, action="ticket-10", version=one["pity_after"]["pity_version"])
        self.assertEqual(self.load()["skill_summon_scrolls"], 0)
        self.assertEqual((len(one["results"]), len(ten["results"])), (1, 10))
        self.assertEqual(self.quest_value("perform_skill_summons"), 11)

    def test_companion_ticket_updates_only_companion_quest_and_pity(self):
        self.update(companion_summon_contracts=1)
        before_skill = normalize_state(self.load()["skill_summon_state_json"])
        with self.force_pull("forest_sprite", "common"):
            result = api.summon_companion({"count": 1, "payment_type": "ticket", "client_action_id": "companion-1",
                                           "expected_pity_version": 1}, "test")
        self.assertEqual(self.load()["companion_summon_contracts"], 0)
        self.assertEqual(result["pity_after"]["total_summons"], 1)
        self.assertEqual(self.quest_value("perform_companion_summons"), 1)
        self.assertEqual(self.quest_value("perform_skill_summons"), 0)
        self.assertEqual(normalize_state(self.load()["skill_summon_state_json"]), before_skill)

    def test_crystal_x1_x10_cost_duplicate_and_no_seed_in_public_api(self):
        self.update(premium_crystals=1000)
        with self.force_pull("spore_strike", "common"):
            one = self.call_skill(payment="premium_crystals", action="crystal-1")
            ten = self.call_skill(10, "premium_crystals", "crystal-10", one["pity_after"]["pity_version"])
            duplicate = self.call_skill(10, "premium_crystals", "crystal-10", one["pity_after"]["pity_version"])
        self.assertEqual((one["premium_crystals_spent"], ten["premium_crystals_spent"]), (100, 900))
        self.assertEqual(self.load()["premium_crystals"], 0); self.assertTrue(duplicate["duplicate_request"])
        public = {"status": api.summon_status("test"), "history": api.summon_history("test"), "result": ten}
        self.assertNotIn("seed", json.dumps(public).lower())

    def test_stale_and_insufficient_currency_change_nothing(self):
        self.update(skill_summon_scrolls=0, premium_crystals=0); before = self.summon_snapshot()
        stale = self.call_skill(action="stale", version=999)
        self.assertTrue(stale["stale_version"]); self.assertEqual(self.summon_snapshot(), before)
        with self.assertRaises(HTTPException): self.call_skill(action="poor")
        self.assertEqual(self.summon_snapshot(), before)

    def test_first_copy_unlock_duplicate_fragments_no_level_change(self):
        self.update(skill_summon_scrolls=2)
        with self.force_pull("spore_strike", "common"):
            first = self.call_skill(action="unlock")
            second = self.call_skill(action="duplicate", version=first["pity_after"]["pity_version"])
        collection = json.loads(self.load()["skills_collection_json"])
        fragments = json.loads(self.load()["skill_fragments_json"])
        self.assertTrue(first["results"][0]["newly_unlocked"]); self.assertTrue(second["results"][0]["duplicate"])
        self.assertEqual(collection["spore_strike"]["level"], 1); self.assertEqual(fragments["spore_strike"], 1)

    def test_summon_rollback_restores_all_state_including_quest(self):
        self.update(skill_summon_scrolls=1); before = self.summon_snapshot(); original = api.QuestProgressTracker.dispatch
        def fail_after_dispatch(tracker, *args, **kwargs):
            original(tracker, *args, **kwargs); raise RuntimeError("summon rollback")
        with patch.object(api, "get_or_create_player", side_effect=self.load), self.force_pull("spore_strike", "common"), patch.object(api.QuestProgressTracker, "dispatch", fail_after_dispatch):
            with self.assertRaisesRegex(RuntimeError, "summon rollback"): self.call_skill(action="rollback")
        self.assertEqual(self.summon_snapshot(), before); self.assertEqual(self.quest_value("perform_skill_summons"), 0)

    def test_history_is_limited_to_100(self):
        state = normalize_state({"summon_history": [{"timestamp": n} for n in range(99)]})
        self.update(skill_summon_scrolls=10, skill_summon_state_json=json.dumps(state))
        with self.force_pull("spore_strike", "common"): self.call_skill(10, action="history")
        self.assertEqual(len(normalize_state(self.load()["skill_summon_state_json"])["summon_history"]), 100)

    # Pity through the transactional API.
    def test_rare_epic_legendary_priority_and_sequential_ten_pull(self):
        cases = (({"pulls_since_rare": 9}, "rare"),
                 ({"pulls_since_epic": 49}, "epic"),
                 ({"pulls_since_rare": 9, "pulls_since_epic": 49, "pulls_since_legendary": 149}, "legendary"))
        for index, (partial, expected) in enumerate(cases):
            state = normalize_state(partial); self.update(skill_summon_scrolls=10, skill_summon_state_json=json.dumps(state), skills_collection_json="{}", skill_fragments_json="{}")
            with patch.object(api, "roll_rarity", side_effect=lambda seed, pull, minimum=None: minimum or "common"):
                result = self.call_skill(10, action=f"pity-{index}")
            guarantees = [row["guarantee_type"] for row in result["results"] if row["guarantee_triggered"]]
            self.assertIn(expected, guarantees)
            if index == 0: self.assertEqual(result["results"][0]["guarantee_type"], "rare")

    def test_skill_and_companion_pity_are_independent(self):
        skill = normalize_state({"pulls_since_rare": 9}); companion = normalize_state({"pulls_since_epic": 17})
        self.update(skill_summon_scrolls=1, skill_summon_state_json=json.dumps(skill), companion_summon_state_json=json.dumps(companion))
        with patch.object(api, "roll_rarity", side_effect=lambda seed, pull, minimum=None: minimum or "common"):
            self.call_skill(action="independent")
        self.assertEqual(normalize_state(self.load()["companion_summon_state_json"]), companion)


if __name__ == "__main__": unittest.main()
