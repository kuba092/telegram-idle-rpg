import datetime as dt
import json
import sqlite3
import unittest

import quest_system as qs


class QuestSystemTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:"); self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE players (telegram_id INTEGER PRIMARY KEY,
          premium_crystals INTEGER DEFAULT 0, skill_tomes INTEGER DEFAULT 0,
          companion_essence INTEGER DEFAULT 0, salvage_dust INTEGER DEFAULT 0,
          refinement_ore INTEGER DEFAULT 0, daily_quests_json TEXT DEFAULT '',
          weekly_quests_json TEXT DEFAULT '', achievements_json TEXT DEFAULT '',
          quest_daily_reset_key TEXT DEFAULT '', quest_weekly_reset_key TEXT DEFAULT '',
          quest_claim_history_json TEXT DEFAULT '{}')""")
        self.db.execute("INSERT INTO players(telegram_id) VALUES(1)")
        self.now = dt.datetime(2026, 8, 3, 12, tzinfo=dt.timezone.utc)
        qs.ensure_state(self.db, 1, self.now)
        self.db.commit()

    def player(self): return dict(self.db.execute("SELECT * FROM players WHERE telegram_id=1").fetchone())

    def test_reset_keys_and_same_period_preserves_progress(self):
        tracker = qs.QuestProgressTracker(self.db, 1, self.now); tracker.dispatch("enemy_defeated", 7)
        qs.ensure_state(self.db, 1, self.now + dt.timedelta(hours=2))
        self.assertEqual(json.loads(self.player()["daily_quests_json"])["quests"][0]["current_value"], 7)
        self.assertEqual(self.player()["quest_daily_reset_key"], "2026-08-03")
        self.assertEqual(self.player()["quest_weekly_reset_key"], "2026-W32")
        qs.ensure_state(self.db, 1, self.now + dt.timedelta(days=1))
        self.assertEqual(json.loads(self.player()["daily_quests_json"])["quests"][0]["current_value"], 0)

    def test_weekly_reset_monday_and_achievements_survive(self):
        tracker=qs.QuestProgressTracker(self.db,1,self.now);tracker.dispatch("stage_reached",10,absolute=True)
        before=json.loads(self.player()["achievements_json"])
        qs.ensure_state(self.db,1,self.now+dt.timedelta(days=7))
        self.assertEqual(json.loads(self.player()["achievements_json"]),before)
        self.assertEqual(self.player()["quest_weekly_reset_key"],"2026-W33")

    def test_elite_and_boss_also_can_count_enemy_once(self):
        tracker=qs.QuestProgressTracker(self.db,1,self.now)
        tracker.dispatch("enemy_defeated",1);tracker.dispatch("elite_defeated",1);tracker.dispatch("boss_defeated",1)
        daily=json.loads(self.player()["daily_quests_json"])["quests"]
        self.assertEqual(next(q for q in daily if q["objective_type"]=="defeat_enemies")["current_value"],1)
        self.assertEqual(next(q for q in daily if q["objective_type"]=="defeat_bosses")["current_value"],1)

    def test_duplicate_action_and_login_once(self):
        tracker=qs.QuestProgressTracker(self.db,1,self.now)
        self.assertTrue(tracker.dispatch("item_salvaged",8,client_action_id="bulk"))
        self.assertFalse(tracker.dispatch("item_salvaged",8,client_action_id="bulk"))
        tracker.dispatch("player_login",5); tracker.dispatch("player_login",5)
        daily=json.loads(self.player()["daily_quests_json"])["quests"]
        self.assertEqual(next(q for q in daily if q["objective_type"]=="salvage_items")["current_value"],8)
        self.assertEqual(next(q for q in daily if q["objective_type"]=="login")["current_value"],1)

    def test_rollback_restores_progress(self):
        self.db.execute("BEGIN");qs.QuestProgressTracker(self.db,1,self.now).dispatch("item_rerolled",3);self.db.rollback()
        self.assertEqual(next(q for q in json.loads(self.player()["achievements_json"])["quests"] if q["objective_type"]=="reroll_items")["current_value"],0)

    def test_first_clear_once_and_jump(self):
        tracker=qs.QuestProgressTracker(self.db,1,self.now)
        self.assertEqual(tracker.first_clear(1,50)["premium_crystals_gained"],30)
        self.assertEqual(tracker.first_clear(1,50)["premium_crystals_gained"],0)
        self.assertEqual(self.player()["premium_crystals"],30)

    def test_caps_are_catalog_invariants(self):
        daily=sum(q["reward"].get("premium_crystals",0) for q in qs.DAILY_CATALOG)+sum(r.get("premium_crystals",0) for r in qs.DAILY_MILESTONES.values())
        weekly=sum(q["reward"].get("premium_crystals",0) for q in qs.WEEKLY_CATALOG)+sum(r.get("premium_crystals",0) for r in qs.WEEKLY_MILESTONES.values())
        self.assertEqual(daily,qs.DAILY_PREMIUM_CAP);self.assertEqual(weekly,qs.WEEKLY_PREMIUM_CAP)

    def test_public_contract_uses_server_time(self):
        block=qs.public_block(self.player(),self.now)
        self.assertEqual(block["server_time"],self.now.isoformat())
        self.assertIn("daily",block);self.assertIn("achievements",block);self.assertFalse(block["currency_sources"]["premium_crystals"]["repeatable_farming"])


if __name__ == "__main__": unittest.main()
