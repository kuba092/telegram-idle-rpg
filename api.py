import hashlib
import hmac
import json
import os
import sqlite3
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware


BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_PATH = "/root/telegram-idle-rpg/game.db"

MAX_ENERGY = 10
ENERGY_RESTORE_SECONDS = 300  # 5 минут

app = FastAPI(title="Telegram Idle RPG API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://kuba092.github.io",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def create_database() -> None:
    connection = get_database()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            level INTEGER NOT NULL DEFAULT 1,
            gold INTEGER NOT NULL DEFAULT 0,
            energy INTEGER NOT NULL DEFAULT 10,
            enemy_hp INTEGER NOT NULL DEFAULT 30,
            updated_at INTEGER NOT NULL,
            energy_updated_at INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(players)"
        ).fetchall()
    }

    if "energy_updated_at" not in columns:
        connection.execute(
            """
            ALTER TABLE players
            ADD COLUMN energy_updated_at INTEGER NOT NULL DEFAULT 0
            """
        )

    connection.execute(
        """
        UPDATE players
        SET energy_updated_at = updated_at
        WHERE energy_updated_at = 0
        """
    )

    connection.commit()
    connection.close()


def validate_telegram_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="BOT_TOKEN не настроен на сервере",
        )

    parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed_data.pop("hash", None)

    if not received_hash:
        raise HTTPException(
            status_code=401,
            detail="Отсутствует подпись Telegram",
        )

    auth_date = int(parsed_data.get("auth_date", "0"))

    if time.time() - auth_date > 86400:
        raise HTTPException(
            status_code=401,
            detail="Данные Telegram устарели",
        )

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(parsed_data.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(
            status_code=401,
            detail="Неверная подпись Telegram",
        )

    user_data = parsed_data.get("user")

    if not user_data:
        raise HTTPException(
            status_code=401,
            detail="Telegram не передал пользователя",
        )

    return json.loads(user_data)


def restore_energy(
    connection: sqlite3.Connection,
    telegram_id: int,
) -> None:
    player = connection.execute(
        """
        SELECT energy, energy_updated_at
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if not player:
        return

    current_energy = player["energy"]

    if current_energy >= MAX_ENERGY:
        connection.execute(
            """
            UPDATE players
            SET energy_updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                int(time.time()),
                telegram_id,
            ),
        )
        return

    now = int(time.time())
    elapsed_seconds = now - player["energy_updated_at"]
    restored_energy = elapsed_seconds // ENERGY_RESTORE_SECONDS

    if restored_energy <= 0:
        return

    new_energy = min(
        MAX_ENERGY,
        current_energy + restored_energy,
    )

    used_seconds = restored_energy * ENERGY_RESTORE_SECONDS
    new_energy_updated_at = (
        player["energy_updated_at"] + used_seconds
    )

    if new_energy >= MAX_ENERGY:
        new_energy_updated_at = now

    connection.execute(
        """
        UPDATE players
        SET energy = ?,
            energy_updated_at = ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (
            new_energy,
            new_energy_updated_at,
            now,
            telegram_id,
        ),
    )


def get_or_create_player(user: dict) -> dict:
    telegram_id = int(user["id"])
    username = user.get("username", "")
    first_name = user.get("first_name", "Игрок")
    now = int(time.time())

    connection = get_database()

    connection.execute(
        """
        INSERT OR IGNORE INTO players (
            telegram_id,
            username,
            first_name,
            updated_at,
            energy_updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            telegram_id,
            username,
            first_name,
            now,
            now,
        ),
    )

    connection.execute(
        """
        UPDATE players
        SET username = ?,
            first_name = ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (
            username,
            first_name,
            now,
            telegram_id,
        ),
    )

    restore_energy(connection, telegram_id)
    connection.commit()

    player = connection.execute(
        """
        SELECT *
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    connection.close()

    return dict(player)


@app.on_event("startup")
def startup_event() -> None:
    create_database()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/player")
def player(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    return get_or_create_player(user)


@app.post("/fight")
def fight(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)

    if player_data["energy"] <= 0:
        return {
            **player_data,
            "message": "😴 Энергия закончилась",
        }

    energy = player_data["energy"] - 1
    enemy_hp = player_data["enemy_hp"] - 10
    gold = player_data["gold"]
    message = "⚔️ Ты нанёс 10 урона"
    now = int(time.time())

    if enemy_hp <= 0:
        enemy_hp = 30
        gold += 10
        message = "🏆 Слизень побеждён! +10 золота"

    connection = get_database()

    connection.execute(
        """
        UPDATE players
        SET gold = ?,
            energy = ?,
            enemy_hp = ?,
            energy_updated_at = ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (
            gold,
            energy,
            enemy_hp,
            now,
            now,
            player_data["telegram_id"],
        ),
    )

    connection.commit()

    updated_player = connection.execute(
        """
        SELECT *
        FROM players
        WHERE telegram_id = ?
        """,
        (player_data["telegram_id"],),
    ).fetchone()

    connection.close()

    return {
        **dict(updated_player),
        "message": message,
    }
