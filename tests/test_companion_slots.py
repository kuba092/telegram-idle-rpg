import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import api


class CompanionSlotsTest(unittest.TestCase):
    telegram_id = 700001
    owned_companions = {
        "forest_sprite": {
            "owned": True,
            "level": 1,
            "fragments": 0,
        },
        "baby_slime": {
            "owned": True,
            "level": 1,
            "fragments": 0,
        },
    }

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = str(
            Path(self.temp_directory.name) / "game-test.db"
        )
        self.database_patch = patch.object(
            api,
            "DATABASE_PATH",
            self.database_path,
        )
        self.auth_patch = patch.object(
            api,
            "validate_telegram_data",
            return_value={
                "id": self.telegram_id,
                "username": "companion_test",
                "first_name": "Test",
            },
        )
        self.database_patch.start()
        self.auth_patch.start()
        api.create_database()
        api.get_or_create_player(
            {
                "id": self.telegram_id,
                "username": "companion_test",
                "first_name": "Test",
            }
        )
        self.set_player_state(level=50)

    def tearDown(self):
        self.auth_patch.stop()
        self.database_patch.stop()
        self.temp_directory.cleanup()

    def set_player_state(self, level, slots=None):
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            """
            UPDATE players
            SET level = ?,
                companions_collection_json = ?,
                companion_slots_json = ?
            WHERE telegram_id = ?
            """,
            (
                level,
                json.dumps(self.owned_companions),
                json.dumps(slots or [None, None, None]),
                self.telegram_id,
            ),
        )
        connection.commit()
        connection.close()

    def stored_slots(self):
        connection = sqlite3.connect(self.database_path)
        value = connection.execute(
            """
            SELECT companion_slots_json
            FROM players
            WHERE telegram_id = ?
            """,
            (self.telegram_id,),
        ).fetchone()[0]
        connection.close()
        return json.loads(value)

    def test_equip_owned_companion(self):
        response = api.equip_companion(
            slot=1,
            companion_id="forest_sprite",
            x_telegram_init_data="test",
        )

        self.assertTrue(response["companion_equipped"])
        self.assertEqual(
            response["companion_system"]["slots"][0][
                "companion_id"
            ],
            "forest_sprite",
        )
        self.assertEqual(
            self.stored_slots(),
            ["forest_sprite", None, None],
        )

    def test_cannot_equip_unowned_companion(self):
        with self.assertRaises(HTTPException) as raised:
            api.equip_companion(
                slot=1,
                companion_id="spore_beetle",
                x_telegram_init_data="test",
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(
            raised.exception.detail,
            "Сначала получите этого спутника",
        )

    def test_cannot_equip_locked_slot(self):
        self.set_player_state(level=10)

        with self.assertRaises(HTTPException) as raised:
            api.equip_companion(
                slot=2,
                companion_id="forest_sprite",
                x_telegram_init_data="test",
            )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIn("25", raised.exception.detail)

    def test_cannot_equip_same_companion_twice(self):
        self.set_player_state(
            level=50,
            slots=["forest_sprite", None, None],
        )

        with self.assertRaises(HTTPException) as raised:
            api.equip_companion(
                slot=2,
                companion_id="forest_sprite",
                x_telegram_init_data="test",
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            self.stored_slots(),
            ["forest_sprite", None, None],
        )

    def test_unequip_companion(self):
        self.set_player_state(
            level=50,
            slots=["forest_sprite", "baby_slime", None],
        )

        response = api.unequip_companion(
            slot=1,
            x_telegram_init_data="test",
        )

        self.assertTrue(response["companion_unequipped"])
        self.assertEqual(
            self.stored_slots(),
            [None, "baby_slime", None],
        )


if __name__ == "__main__":
    unittest.main()
