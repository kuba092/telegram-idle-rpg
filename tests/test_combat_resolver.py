import sqlite3
import unittest

from combat_effects import CombatContext
from combat_resolver import CombatResolution, CombatResolver


class CombatResolverTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE players (telegram_id INTEGER PRIMARY KEY, enemy_hp INTEGER)")
        self.db.execute("INSERT INTO players VALUES (1, 10)")
        self.db.commit()
        self.player = {"telegram_id": 1, "enemy_hp": 10, "enemy_max_hp": 10, "battle": "a"}
        self.rewards = 0

    def resolver(self, identity="a", resolution=None, fail=False):
        def victory(connection, player, boss, context):
            self.rewards += 1
            connection.execute("UPDATE players SET enemy_hp=99 WHERE telegram_id=1")
            if fail:
                raise RuntimeError("reward failure")
            player.update(enemy_hp=10, battle="b")
            return {"player": player, "reward_granted": True,
                    "next_battle_identity": "b", "temporary_effects_cleared": True}
        return CombatResolver(self.db, self.player, identity, lambda p: p["battle"], victory, resolution)

    def event(self, resolver, damage, source="normal"):
        return resolver.resolve(
            damage_source=source, attack_source=source, raw_damage=damage,
            crit_metadata={}, boss=False,
            context=CombatContext(10, 10, self.player["enemy_hp"]),
        )

    def test_nonlethal_clamps_and_persists_hp(self):
        result = self.event(self.resolver(), 4)
        self.assertEqual((result.enemy_hp_before, result.enemy_hp_after), (10, 6))
        self.assertEqual(self.db.execute("SELECT enemy_hp FROM players").fetchone()[0], 6)

    def test_first_lethal_rewards_once_and_later_event_is_skipped(self):
        resolution = CombatResolution()
        resolver = self.resolver(resolution=resolution)
        lethal = self.event(resolver, 50, "combo")
        skipped = self.event(resolver, 50, "companion")
        self.assertTrue(lethal.lethal)
        self.assertTrue(lethal.reward_granted)
        self.assertTrue(skipped.skipped_after_kill)
        self.assertEqual(self.rewards, 1)
        self.assertEqual(resolution.public()["lethal_source"], "combo")

    def test_stale_identity_never_damages_or_rewards(self):
        result = self.event(self.resolver(identity="old"), 50)
        self.assertTrue(result.stale_battle)
        self.assertEqual(self.rewards, 0)

    def test_reward_error_is_database_rollback_safe(self):
        self.db.execute("BEGIN")
        with self.assertRaises(RuntimeError):
            self.event(self.resolver(fail=True), 50)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT enemy_hp FROM players").fetchone()[0], 10)


if __name__ == "__main__":
    unittest.main()
