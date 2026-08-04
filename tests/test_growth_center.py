import copy
import unittest

from growth_center import (
    CACHE_TTL_SECONDS, blocker, build_growth_center, clear_growth_center_cache,
    detect_better_items, invalidate_growth_center,
)


class GrowthCenterTest(unittest.TestCase):
    def setUp(self):
        clear_growth_center_cache()
        self.player = {
            "telegram_id": 71, "hero_hp": 100, "chest_level": 1, "chest_xp": 0,
            "gold": 10000, "skill_tomes": 100, "companion_essence": 100,
            "premium_crystals": 10000, "comparison_profile": "balanced",
        }
        self.skills = {"a": {"owned": True, "level": 1}}
        self.companions = {"c": {"owned": True, "level": 1}}
        self.skill_catalog = {"a": {"rarity": "common"}}
        self.companion_catalog = {"c": {"rarity": "common"}}

    def build(self, player=None, inventory=None, equipment=None, now=1000):
        return build_growth_center(player or self.player, skill_collection=self.skills,
            companion_collection=self.companions, skill_slots=["a"], companion_slots=["c"],
            skill_catalog=self.skill_catalog, companion_catalog=self.companion_catalog,
            inventory=inventory or [], equipment=equipment or {}, now=now)

    def test_legacy_inventory_never_creates_a_false_critical_action(self):
        inventory = [{"item_id": str(i), "slot": "weapon"} for i in range(50)]
        center = self.build(player={**self.player, "chests": 1}, inventory=inventory)["growth_center"]
        self.assertNotEqual("inventory_full", center["priority_action"]["action_id"])
        self.assertFalse(any("crystal" in item["action_id"] for item in center["recommended_actions"]))
        self.assertFalse(any(item["type"].startswith("inventory_") for item in center["notifications"]["items"]))
        self.assertFalse(any(item["resource"] == "inventory_space" for item in center["blockers"]))
        open_chest = next(item for item in center["quick_actions"] if item["action_id"] == "open_chest")
        self.assertTrue(open_chest["available"])

    def test_pending_loot_is_highest_priority_and_blocks_only_next_open(self):
        player = {**self.player, "pending_loot": {"item_id": "pending-1", "slot": "weapon"}, "chests": 2}
        center = self.build(player=player)["growth_center"]
        self.assertEqual("resolve_pending_loot", center["priority_action"]["action_id"])
        pending = next(item for item in center["notifications"]["items"] if item["type"] == "pending_loot")
        self.assertEqual("critical", pending["severity"])
        open_chest = next(item for item in center["quick_actions"] if item["action_id"] == "open_chest")
        self.assertFalse(open_chest["available"])

    def test_ticket_summon_precedes_battle_and_order_is_stable(self):
        player = {**self.player, "skill_summon_scrolls": 1}
        first = self.build(player)["growth_center"]
        clear_growth_center_cache()
        second = self.build(player)["growth_center"]
        self.assertEqual(first["priority_action"], second["priority_action"])
        actions = [first["priority_action"]["action_id"], *[x["action_id"] for x in first["recommended_actions"]]]
        self.assertLess(actions.index("summon_skill_ticket"), actions.index("continue_battle"))

    def test_better_item_profile_locked_legacy_and_cap(self):
        equipment = {"weapon": {"item_id": "eq", "slot": "weapon", "damage": 10}}
        inventory = [{"id": "locked", "slot": "weapon", "damage": 30, "locked": True}]
        inventory.extend({"id": f"x{i}", "slot": "weapon", "damage": 0} for i in range(120))
        result = detect_better_items(inventory, equipment, "damage")
        self.assertEqual(100, result["scanned_count"])
        self.assertEqual("locked", result["best_item"]["item_id"])
        self.assertTrue(result["best_item"]["locked"])
        self.assertFalse(result["auto_equipped"])

    def test_cache_ttl_invalidation_and_player_isolation(self):
        one = self.build(now=1000)
        changed = {**self.player, "gold": 0}
        cached = self.build(changed, now=1005)
        self.assertEqual(one, cached)
        invalidate_growth_center(71)
        fresh = self.build(changed, now=1005)
        self.assertNotEqual(one["growth_center"]["blockers"], fresh["growth_center"]["blockers"])
        expired = self.build(self.player, now=1005 + CACHE_TTL_SECONDS)
        self.assertNotEqual(fresh["refreshed_at"], expired["refreshed_at"])
        other = {**self.player, "telegram_id": 72}
        self.assertEqual(72, other["telegram_id"])
        self.assertNotEqual(expired["refreshed_at"], self.build(other, now=2000)["refreshed_at"])

    def test_daily_flow_unique_and_quick_payloads(self):
        center = self.build()["growth_center"]
        steps = center["daily_flow"]["steps"]
        self.assertEqual(len(steps), len({step["step_id"] for step in steps}))
        self.assertGreaterEqual(center["daily_flow"]["completion_percent"], 0)
        self.assertLessEqual(center["daily_flow"]["completion_percent"], 100)
        self.assertTrue(any(step["status"] == "optional" for step in steps))
        for action in center["quick_actions"]:
            self.assertIsInstance(action["payload_template"], dict)

    def test_blocker_values_are_exact(self):
        value = blocker("gold", 25, 10, "upgrade_skill")
        self.assertEqual({"required": 25, "current": 10, "missing": 15},
                         {key: value[key] for key in ("required", "current", "missing")})


if __name__ == "__main__":
    unittest.main()
