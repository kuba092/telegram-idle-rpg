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

BASE_ENEMY_HP = 30
BASE_ENEMY_DAMAGE = 3
BASE_HERO_HP = 100
ENEMIES_PER_STAGE = 10
MIN_ATTACK_INTERVAL = 0.1
ENEMY_ATTACK_SPEED = 0.5
MIN_ENEMY_ATTACK_INTERVAL = 0.5

app = FastAPI(title="Telegram Idle RPG API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://kuba092.github.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def add_column_if_missing(
    connection: sqlite3.Connection,
    column_name: str,
    column_definition: str,
) -> None:
    columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(players)"
        ).fetchall()
    }

    if column_name not in columns:
        connection.execute(
            f"ALTER TABLE players ADD COLUMN "
            f"{column_name} {column_definition}"
        )


def calculate_enemy_hp(stage: int) -> int:
    return round(BASE_ENEMY_HP * (1.22 ** (stage - 1)))


def calculate_enemy_damage(stage: int) -> int:
    return max(
        1,
        round(BASE_ENEMY_DAMAGE * (1.12 ** (stage - 1))),
    )


def calculate_gold_reward(stage: int) -> int:
    return 5 + stage * 2


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
            enemy_hp INTEGER NOT NULL DEFAULT 30,
            updated_at INTEGER NOT NULL
        )
        """
    )

    columns = (
        ("damage", "INTEGER NOT NULL DEFAULT 10"),
        ("attack_speed", "REAL NOT NULL DEFAULT 1.0"),
        ("last_attack_at", "REAL NOT NULL DEFAULT 0"),
        ("stage", "INTEGER NOT NULL DEFAULT 1"),
        ("kills_in_stage", "INTEGER NOT NULL DEFAULT 0"),
        ("total_kills", "INTEGER NOT NULL DEFAULT 0"),
        ("enemy_max_hp", "INTEGER NOT NULL DEFAULT 30"),
        ("power", "INTEGER NOT NULL DEFAULT 10"),
        ("hero_hp", f"INTEGER NOT NULL DEFAULT {BASE_HERO_HP}"),
        ("hero_max_hp", f"INTEGER NOT NULL DEFAULT {BASE_HERO_HP}"),
        ("last_enemy_attack_at", "REAL NOT NULL DEFAULT 0"),
        ("defeats", "INTEGER NOT NULL DEFAULT 0"),
    )

    for column_name, definition in columns:
        add_column_if_missing(
            connection,
            column_name,
            definition,
        )

    connection.execute(
        """
        UPDATE players
        SET enemy_max_hp = 30
        WHERE enemy_max_hp <= 0
        """
    )
    connection.execute(
        """
        UPDATE players
        SET enemy_hp = enemy_max_hp
        WHERE enemy_hp < 0
        """
    )
    connection.execute(
        """
        UPDATE players
        SET hero_max_hp = ?
        WHERE hero_max_hp <= 0
        """,
        (BASE_HERO_HP,),
    )
    connection.execute(
        """
        UPDATE players
        SET hero_hp = hero_max_hp
        WHERE hero_hp > hero_max_hp
        """
    )

    connection.commit()
    connection.close()


def validate_telegram_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="BOT_TOKEN не настроен",
        )

    parsed_data = dict(
        parse_qsl(init_data, keep_blank_values=True)
    )

    received_hash = parsed_data.pop("hash", None)

    if not received_hash:
        raise HTTPException(
            status_code=401,
            detail="Нет подписи Telegram",
        )

    try:
        auth_date = int(parsed_data.get("auth_date", "0"))
    except ValueError as error:
        raise HTTPException(
            status_code=401,
            detail="Некорректная дата Telegram",
        ) from error

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

    if not hmac.compare_digest(
        calculated_hash,
        received_hash,
    ):
        raise HTTPException(
            status_code=401,
            detail="Неверная подпись Telegram",
        )

    user_data = parsed_data.get("user")
    if not user_data:
        raise HTTPException(
            status_code=401,
            detail="Telegram не передал игрока",
        )

    return json.loads(user_data)


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
            updated_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            telegram_id,
            username,
            first_name,
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


def load_player(
    connection: sqlite3.Connection,
    telegram_id: int,
) -> dict:
    player = connection.execute(
        """
        SELECT *
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if player is None:
        raise HTTPException(
            status_code=404,
            detail="Игрок не найден",
        )

    return dict(player)


def build_player_response(
    player: dict,
    **extra,
) -> dict:
    attack_speed = max(
        0.1,
        float(player["attack_speed"]),
    )
    attack_interval = max(
        MIN_ATTACK_INTERVAL,
        1 / attack_speed,
    )

    enemy_attack_interval = max(
        MIN_ENEMY_ATTACK_INTERVAL,
        1 / ENEMY_ATTACK_SPEED,
    )
    stage = int(player["stage"])

    return {
        **player,
        "attack_interval": attack_interval,
        "enemy_damage": calculate_enemy_damage(stage),
        "enemy_attack_speed": ENEMY_ATTACK_SPEED,
        "enemy_attack_interval": enemy_attack_interval,
        "hero_alive": int(player["hero_hp"]) > 0,
        **extra,
    }


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
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)

    return build_player_response(player_data)


@app.post("/attack")
def attack(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)

    if int(player_data["hero_hp"]) <= 0:
        return build_player_response(
            player_data,
            attacked=False,
            hero_defeated=True,
            message="💀 Герой ожидает возрождения",
        )

    now = time.time()
    attack_speed = max(
        0.1,
        float(player_data["attack_speed"]),
    )
    attack_interval = max(
        MIN_ATTACK_INTERVAL,
        1 / attack_speed,
    )

    elapsed = now - float(
        player_data["last_attack_at"]
    )
    if (
        player_data["last_attack_at"] > 0
        and elapsed < attack_interval
    ):
        retry_after = attack_interval - elapsed

        return build_player_response(
            player_data,
            attacked=False,
            retry_after=retry_after,
            message="Атака ещё не готова",
        )

    damage = int(player_data["damage"])
    enemy_hp = int(player_data["enemy_hp"]) - damage
    enemy_max_hp = int(player_data["enemy_max_hp"])
    stage = int(player_data["stage"])
    kills_in_stage = int(
        player_data["kills_in_stage"]
    )
    total_kills = int(player_data["total_kills"])
    gold = int(player_data["gold"])

    enemy_defeated = enemy_hp <= 0
    stage_completed = False
    reward = 0
    last_enemy_attack_at = float(
        player_data["last_enemy_attack_at"]
    )

    if enemy_defeated:
        reward = calculate_gold_reward(stage)
        gold += reward
        kills_in_stage += 1
        total_kills += 1

        if kills_in_stage >= ENEMIES_PER_STAGE:
            stage += 1
            kills_in_stage = 0
            stage_completed = True

        enemy_max_hp = calculate_enemy_hp(stage)
        enemy_hp = enemy_max_hp
        last_enemy_attack_at = now

    connection = get_database()
    connection.execute(
        """
        UPDATE players
        SET gold = ?,
            enemy_hp = ?,
            enemy_max_hp = ?,
            stage = ?,
            kills_in_stage = ?,
            total_kills = ?,
            last_attack_at = ?,
            last_enemy_attack_at = ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (
            gold,
            enemy_hp,
            enemy_max_hp,
            stage,
            kills_in_stage,
            total_kills,
            now,
            last_enemy_attack_at,
            int(now),
            player_data["telegram_id"],
        ),
    )
    connection.commit()
    updated_player = load_player(
        connection,
        int(player_data["telegram_id"]),
    )
    connection.close()

    message = f"⚔️ Нанесено {damage} урона"

    if enemy_defeated:
        message = (
            f"🏆 Враг побеждён! +{reward} золота"
        )

    if stage_completed:
        message = (
            f"🚪 Этап пройден! Открыт этап {stage}"
        )

    return build_player_response(
        updated_player,
        attacked=True,
        enemy_defeated=enemy_defeated,
        stage_completed=stage_completed,
        reward=reward,
        message=message,
    )


@app.post("/enemy-attack")
def enemy_attack(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)

    if int(player_data["hero_hp"]) <= 0:
        return build_player_response(
            player_data,
            enemy_attacked=False,
            hero_defeated=True,
            message="💀 Герой повержен",
        )

    now = time.time()
    enemy_attack_interval = max(
        MIN_ENEMY_ATTACK_INTERVAL,
        1 / ENEMY_ATTACK_SPEED,
    )
    elapsed = now - float(
        player_data["last_enemy_attack_at"]
    )

    if (
        player_data["last_enemy_attack_at"] > 0
        and elapsed < enemy_attack_interval
    ):
        retry_after = enemy_attack_interval - elapsed

        return build_player_response(
            player_data,
            enemy_attacked=False,
            retry_after=retry_after,
            message="Атака врага ещё не готова",
        )

    stage = int(player_data["stage"])
    enemy_damage = calculate_enemy_damage(stage)
    hero_hp = max(
        0,
        int(player_data["hero_hp"]) - enemy_damage,
    )
    hero_defeated = hero_hp <= 0
    defeats = int(player_data["defeats"])

    if hero_defeated:
        defeats += 1

    connection = get_database()
    connection.execute(
        """
        UPDATE players
        SET hero_hp = ?,
            defeats = ?,
            last_enemy_attack_at = ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (
            hero_hp,
            defeats,
            now,
            int(now),
            player_data["telegram_id"],
        ),
    )
    connection.commit()
    updated_player = load_player(
        connection,
        int(player_data["telegram_id"]),
    )
    connection.close()

    message = f"🩸 Враг нанёс {enemy_damage} урона"
    if hero_defeated:
        message = "💀 Герой повержен"

    return build_player_response(
        updated_player,
        enemy_attacked=True,
        hero_defeated=hero_defeated,
        received_damage=enemy_damage,
        message=message,
    )


@app.post("/respawn")
def respawn(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)

    if int(player_data["hero_hp"]) > 0:
        return build_player_response(
            player_data,
            respawned=False,
            message="Герой уже в бою",
        )

    now = time.time()
    hero_max_hp = int(player_data["hero_max_hp"])
    enemy_max_hp = int(player_data["enemy_max_hp"])

    connection = get_database()
    connection.execute(
        """
        UPDATE players
        SET hero_hp = ?,
            enemy_hp = ?,
            last_attack_at = ?,
            last_enemy_attack_at = ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (
            hero_max_hp,
            enemy_max_hp,
            now,
            now,
            int(now),
            player_data["telegram_id"],
        ),
    )
    connection.commit()
    updated_player = load_player(
        connection,
        int(player_data["telegram_id"]),
    )
    connection.close()

    return build_player_response(
        updated_player,
        respawned=True,
        message="✨ Герой возродился",
    )
