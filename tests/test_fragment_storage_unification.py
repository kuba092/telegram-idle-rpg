import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import api
from summon_system import normalize_state


PLAYER_ID = 99117


class FragmentStorageUnificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(api, "DATABASE_PATH", str(Path(self.tmp.name) / "game.db"))
        self.auth_patch = patch.object(api, "validate_telegram_data", return_value={"id": PLAYER_ID, "first_name": "Fragments"})
        self.db_patch.start(); self.auth_patch.start(); api.ACTION_CACHE.clear(); api.create_database()
        api.get_or_create_player({"id": PLAYER_ID, "first_name": "Fragments"})

    def tearDown(self):
        api.ACTION_CACHE.clear(); self.auth_patch.stop(); self.db_patch.stop(); self.tmp.cleanup()

    def load(self):
        connection = api.get_database()
        try: return api.load_player(connection, PLAYER_ID)
        finally: connection.close()

    def update(self, **values):
        connection = api.get_database()
        try:
            connection.execute("UPDATE players SET " + ",".join(f"{key}=?" for key in values) + " WHERE telegram_id=?", (*values.values(), PLAYER_ID))
            connection.commit()
        finally: connection.close()

    def test_legacy_skill_duplicate_credits_canonical_balance_and_rank_sees_it(self):
        entry = {"owned": True, "level": 7, "rank": 0, "rank_stars": 0, "awakening_tier": 0, "rank_version": 1}
        self.update(level=50, skill_scrolls=1, premium_crystals=73,
                    skills_collection_json=json.dumps({"spore_strike": entry}),
                    skill_fragments_json=json.dumps({"spore_strike": 4}))
        with patch.object(api, "roll_skill_id", return_value="spore_strike"):
            result = api.summon_skills(1, "test")
        stored = self.load(); collection = json.loads(stored["skills_collection_json"]); balances = json.loads(stored["skill_fragments_json"])
        self.assertEqual(collection["spore_strike"]["level"], 7)
        self.assertEqual((collection["spore_strike"]["rank"], collection["spore_strike"]["rank_stars"]), (0, 0))
        self.assertNotIn("fragments", collection["spore_strike"])
        self.assertEqual(result["summon_results"][0]["fragments"], balances["spore_strike"])
        ranked = api.rank_up_skill({"entity_id": "spore_strike", "expected_rank_version": 1, "client_action_id": "rank-after-legacy"}, "test")
        self.assertTrue(ranked["transaction_completed"]); self.assertEqual(ranked["new_stars"], 1)
        self.assertEqual(self.load()["premium_crystals"], 73)

    def test_legacy_companion_duplicate_credits_canonical_balance_without_level_or_rank(self):
        entry = {"owned": True, "level": 9, "rank": 1, "rank_stars": 2, "awakening_tier": 0, "rank_version": 4}
        self.update(level=50, companion_scrolls=1, premium_crystals=81,
                    companions_collection_json=json.dumps({"forest_sprite": entry}), companion_fragments_json="{}")
        with patch.object(api, "roll_companion_id", return_value="forest_sprite"):
            result = api.summon_companions(1, "test")
        stored = self.load(); collection = json.loads(stored["companions_collection_json"]); balances = json.loads(stored["companion_fragments_json"])
        self.assertEqual((collection["forest_sprite"]["level"], collection["forest_sprite"]["rank"], collection["forest_sprite"]["rank_stars"]), (9, 1, 2))
        self.assertNotIn("fragments", collection["forest_sprite"])
        self.assertEqual(result["companion_summon_results"][0]["fragments"], balances["forest_sprite"])
        self.assertEqual(stored["premium_crystals"], 81)

    def test_embedded_fragments_migrate_once_and_preserve_progression(self):
        entry = {"owned": True, "level": 13, "fragments": 17, "rank": 2, "rank_stars": 3,
                 "awakening_tier": 0, "rank_version": 8}
        self.update(skills_collection_json=json.dumps({"spore_strike": entry}),
                    skill_fragments_json=json.dumps({"spore_strike": 4}))
        api.get_or_create_player({"id": PLAYER_ID}); first = self.load()
        api.get_or_create_player({"id": PLAYER_ID}); second = self.load()
        first_collection = json.loads(first["skills_collection_json"]); second_collection = json.loads(second["skills_collection_json"])
        self.assertEqual(json.loads(first["skill_fragments_json"])["spore_strike"], 21)
        self.assertEqual(json.loads(second["skill_fragments_json"])["spore_strike"], 21)
        self.assertNotIn("fragments", second_collection["spore_strike"])
        self.assertEqual(tuple(second_collection["spore_strike"][key] for key in ("level", "rank", "rank_stars", "awakening_tier", "rank_version")), (13, 2, 3, 0, 8))

    def test_new_and_legacy_summon_share_balance(self):
        entry = {"owned": True, "level": 3, "rank_version": 1}
        self.update(level=50, skill_scrolls=1, skill_summon_scrolls=1,
                    skills_collection_json=json.dumps({"spore_strike": entry}), skill_fragments_json="{}")
        with patch.object(api, "roll_skill_id", return_value="spore_strike"):
            api.summon_skills(1, "test")
        after_legacy = json.loads(self.load()["skill_fragments_json"])["spore_strike"]
        with patch.multiple(api, roll_rarity=lambda *args, **kwargs: "common",
                            choose_entity=lambda *args, **kwargs: ("spore_strike", "common")):
            version = normalize_state(self.load()["skill_summon_state_json"])["pity_version"]
            api.summon_skill({"count": 1, "payment_type": "ticket", "client_action_id": "unified", "expected_pity_version": version}, "test")
        after_new = json.loads(self.load()["skill_fragments_json"])["spore_strike"]
        self.assertEqual(after_new, after_legacy + api.SUMMON_FRAGMENTS["common"])

    def test_legacy_rollback_restores_fragments_scrolls_quest_and_premium(self):
        entry = {"owned": True, "level": 4, "rank_version": 1}
        self.update(level=50, skill_scrolls=1, premium_crystals=66,
                    skills_collection_json=json.dumps({"spore_strike": entry}), skill_fragments_json="{}")
        before = self.load()
        with patch.object(api, "roll_skill_id", return_value="spore_strike"), patch.object(api.QuestProgressTracker, "dispatch", side_effect=RuntimeError("rollback")):
            with self.assertRaisesRegex(RuntimeError, "rollback"): api.summon_skills(1, "test")
        after = self.load()
        for field in ("skill_fragments_json", "skills_collection_json", "skill_scrolls", "skill_summon_exp", "daily_quests_json", "premium_crystals"):
            self.assertEqual(after[field], before[field])


if __name__ == "__main__": unittest.main()
