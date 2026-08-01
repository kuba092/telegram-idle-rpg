import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import time
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware


BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/root/telegram-idle-rpg/game.db",
)

BASE_ENEMY_HP = 30
BASE_ENEMY_DAMAGE = 3
BASE_HERO_HP = 100
BASE_HERO_DAMAGE = 10
BASE_POWER = 10
ENEMIES_PER_STAGE = 10
MIN_ATTACK_INTERVAL = 0.1
ENEMY_ATTACK_SPEED = 0.5
MIN_ENEMY_ATTACK_INTERVAL = 0.5
STARTER_CHESTS = 3
MAX_CHEST_LEVEL = 20
CHEST_UPGRADE_BASE_COST = 400
CHEST_UPGRADE_COST_GROWTH = 1.6

GEAR_SLOTS = {
    "helmet": {
        "name": "Шлем",
        "icon": "🪖",
        "item": "шлем",
        "focus": "defense",
    },
    "armor": {
        "name": "Нагрудник",
        "icon": "🛡️",
        "item": "доспех",
        "focus": "defense",
    },
    "gloves": {
        "name": "Перчатки",
        "icon": "🧤",
        "item": "перчатки",
        "focus": "mixed",
    },
    "pants": {
        "name": "Штаны",
        "icon": "👖",
        "item": "поножи",
        "focus": "defense",
    },
    "boots": {
        "name": "Обувь",
        "icon": "🥾",
        "item": "сапоги",
        "focus": "defense",
    },
    "weapon": {
        "name": "Оружие",
        "icon": "🗡️",
        "item": "клинок",
        "focus": "attack",
    },
    "necklace": {
        "name": "Ожерелье",
        "icon": "📿",
        "item": "ожерелье",
        "focus": "mixed",
    },
    "ring": {
        "name": "Кольцо",
        "icon": "💍",
        "item": "кольцо",
        "focus": "attack",
    },
    "belt": {
        "name": "Пояс",
        "icon": "🧷",
        "item": "пояс",
        "focus": "defense",
    },
    "talisman": {
        "name": "Талисман",
        "icon": "🔮",
        "item": "талисман",
        "focus": "attack",
    },
}

RARITIES = (
    {
        "key": "common",
        "name": "Обычный",
        "adjective": "Походный",
        "multiplier": 1.0,
        "sell_multiplier": 1.0,
    },
    {
        "key": "uncommon",
        "name": "Необычный",
        "adjective": "Улучшенный",
        "multiplier": 1.28,
        "sell_multiplier": 1.2,
    },
    {
        "key": "rare",
        "name": "Редкий",
        "adjective": "Зачарованный",
        "multiplier": 1.65,
        "sell_multiplier": 1.55,
    },
    {
        "key": "epic",
        "name": "Эпический",
        "adjective": "Героический",
        "multiplier": 2.35,
        "sell_multiplier": 2.1,
    },
    {
        "key": "legendary",
        "name": "Легендарный",
        "adjective": "Легендарный",
        "multiplier": 3.45,
        "sell_multiplier": 3.1,
    },
    {
        "key": "mythic",
        "name": "Мифический",
        "adjective": "Мифический",
        "multiplier": 5.0,
        "sell_multiplier": 4.5,
    },
)


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


def parse_json_object(value: object) -> dict:
    if not value:
        return {}

    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}

    return parsed if isinstance(parsed, dict) else {}


def calculate_enemy_hp(stage: int) -> int:
    return round(BASE_ENEMY_HP * (1.22 ** (stage - 1)))


def calculate_enemy_damage(stage: int) -> int:
    return max(
        1,
        round(BASE_ENEMY_DAMAGE * (1.12 ** (stage - 1))),
    )


def calculate_gold_reward(stage: int) -> int:
    return 5 + stage * 2


def calculate_chest_upgrade_cost(chest_level: int) -> int | None:
    chest_level = max(1, int(chest_level))
    if chest_level >= MAX_CHEST_LEVEL:
        return None

    return max(
        1,
        round(
            CHEST_UPGRADE_BASE_COST
            * (CHEST_UPGRADE_COST_GROWTH ** (chest_level - 1))
        ),
    )


def chest_rarity_weights(chest_level: int) -> list[float]:
    chest_level = max(1, min(MAX_CHEST_LEVEL, int(chest_level)))

    if chest_level <= 3:
        return [72, 25, 3, 0, 0, 0]
    if chest_level <= 6:
        return [55, 32, 11, 2, 0, 0]
    if chest_level <= 9:
        return [40, 35, 20, 4.5, 0.5, 0]
    if chest_level <= 12:
        return [28, 34, 27, 9, 2, 0]
    if chest_level <= 15:
        return [18, 30, 31, 16, 4.5, 0.5]
    if chest_level <= 18:
        return [10, 24, 33, 23, 8, 2]

    return [5, 18, 32, 28, 13, 4]


def chest_rarity_chances(chest_level: int) -> dict:
    weights = chest_rarity_weights(chest_level)
    total = sum(weights) or 1

    return {
        rarity["key"]: round(weight * 100 / total, 2)
        for rarity, weight in zip(RARITIES, weights)
    }


def calculate_equipment_stats(equipment: dict) -> dict:
    power_bonus = 0
    damage_bonus = 0
    hp_bonus = 0

    for item in equipment.values():
        if not isinstance(item, dict):
            continue

        power_bonus += max(0, int(item.get("power", 0)))
        damage_bonus += max(0, int(item.get("damage", 0)))
        hp_bonus += max(0, int(item.get("hp", 0)))

    return {
        "power": BASE_POWER + power_bonus,
        "damage": BASE_HERO_DAMAGE + damage_bonus,
        "hero_max_hp": BASE_HERO_HP + hp_bonus,
    }


def sync_player_stats(
    connection: sqlite3.Connection,
    telegram_id: int,
) -> None:
    row = connection.execute(
        """
        SELECT equipment_json, hero_hp, hero_max_hp,
               damage, power
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if row is None:
        return

    equipment = parse_json_object(row["equipment_json"])
    stats = calculate_equipment_stats(equipment)
    current_max_hp = max(1, int(row["hero_max_hp"]))
    current_hp = max(0, int(row["hero_hp"]))
    max_hp_gain = stats["hero_max_hp"] - current_max_hp

    if max_hp_gain > 0:
        current_hp += max_hp_gain

    current_hp = min(current_hp, stats["hero_max_hp"])

    connection.execute(
        """
        UPDATE players
        SET damage = ?,
            power = ?,
            hero_max_hp = ?,
            hero_hp = ?
        WHERE telegram_id = ?
        """,
        (
            stats["damage"],
            stats["power"],
            stats["hero_max_hp"],
            current_hp,
            telegram_id,
        ),
    )


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
        ("damage", f"INTEGER NOT NULL DEFAULT {BASE_HERO_DAMAGE}"),
        ("attack_speed", "REAL NOT NULL DEFAULT 1.0"),
        ("last_attack_at", "REAL NOT NULL DEFAULT 0"),
        ("stage", "INTEGER NOT NULL DEFAULT 1"),
        ("kills_in_stage", "INTEGER NOT NULL DEFAULT 0"),
        ("total_kills", "INTEGER NOT NULL DEFAULT 0"),
        ("enemy_max_hp", f"INTEGER NOT NULL DEFAULT {BASE_ENEMY_HP}"),
        ("power", f"INTEGER NOT NULL DEFAULT {BASE_POWER}"),
        ("hero_hp", f"INTEGER NOT NULL DEFAULT {BASE_HERO_HP}"),
        ("hero_max_hp", f"INTEGER NOT NULL DEFAULT {BASE_HERO_HP}"),
        ("last_enemy_attack_at", "REAL NOT NULL DEFAULT 0"),
        ("defeats", "INTEGER NOT NULL DEFAULT 0"),
        ("chests", f"INTEGER NOT NULL DEFAULT {STARTER_CHESTS}"),
        ("chest_level", "INTEGER NOT NULL DEFAULT 1"),
        ("equipment_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("pending_loot_json", "TEXT NOT NULL DEFAULT ''"),
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
        SET enemy_max_hp = ?
        WHERE enemy_max_hp <= 0
        """,
        (BASE_ENEMY_HP,),
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

    player_ids = connection.execute(
        "SELECT telegram_id FROM players"
    ).fetchall()
    for row in player_ids:
        sync_player_stats(connection, int(row["telegram_id"]))

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
    sync_player_stats(connection, telegram_id)
    connection.commit()
    player = load_player(connection, telegram_id)
    connection.close()

    return player


def public_equipment(player: dict) -> dict:
    stored = parse_json_object(player.get("equipment_json"))
    return {
        slot_key: stored.get(slot_key)
        for slot_key in GEAR_SLOTS
    }


def public_pending_loot(player: dict) -> dict | None:
    pending = parse_json_object(player.get("pending_loot_json"))
    return pending or None


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

    hidden_fields = {
        "equipment_json",
        "pending_loot_json",
    }
    public_player = {
        key: value
        for key, value in player.items()
        if key not in hidden_fields
    }

    return {
        **public_player,
        "equipment": public_equipment(player),
        "pending_loot": public_pending_loot(player),
        "attack_interval": attack_interval,
        "enemy_damage": calculate_enemy_damage(stage),
        "enemy_attack_speed": ENEMY_ATTACK_SPEED,
        "enemy_attack_interval": enemy_attack_interval,
        "hero_alive": int(player["hero_hp"]) > 0,
        "chest_level": max(1, int(player.get("chest_level", 1))),
        "chest_max_level": MAX_CHEST_LEVEL,
        "chest_upgrade_cost": calculate_chest_upgrade_cost(
            int(player.get("chest_level", 1))
        ),
        "chest_rarity_chances": chest_rarity_chances(
            int(player.get("chest_level", 1))
        ),
        **extra,
    }


def choose_rarity(chest_level: int) -> dict:
    weights = chest_rarity_weights(chest_level)
    indexes = list(range(len(RARITIES)))
    chosen_index = random.choices(
        indexes,
        weights=weights,
        k=1,
    )[0]
    return RARITIES[chosen_index]


def generate_loot(stage: int, chest_level: int) -> dict:
    slot_key = random.choice(list(GEAR_SLOTS))
    slot = GEAR_SLOTS[slot_key]
    chest_level = max(1, min(MAX_CHEST_LEVEL, int(chest_level)))
    rarity = choose_rarity(chest_level)
    stage = max(1, int(stage))

    base_roll = stage + random.randint(
        0,
        max(2, stage // 2 + 1),
    )
    chest_power_multiplier = 1 + (chest_level - 1) * 0.075
    item_power = max(
        1,
        round(
            base_roll
            * chest_power_multiplier
            * rarity["multiplier"]
        ),
    )

    damage_bonus = 0
    hp_bonus = 0
    if slot["focus"] == "attack":
        damage_bonus = max(1, round(item_power * 0.55))
    elif slot["focus"] == "defense":
        hp_bonus = max(5, item_power * 4)
    else:
        damage_bonus = max(1, round(item_power * 0.28))
        hp_bonus = max(3, item_power * 2)

    sell_price = max(
        3,
        round(
            item_power
            * rarity["sell_multiplier"]
            * 3
        ),
    )

    return {
        "id": secrets.token_hex(8),
        "slot": slot_key,
        "slot_name": slot["name"],
        "icon": slot["icon"],
        "name": (
            f"{rarity['adjective']} "
            f"{slot['item']}"
        ),
        "rarity": rarity["key"],
        "rarity_name": rarity["name"],
        "power": item_power,
        "damage": damage_bonus,
        "hp": hp_bonus,
        "sell_price": sell_price,
        "stage_found": stage,
        "chest_level_found": chest_level,
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
    chests = int(player_data["chests"])

    enemy_defeated = enemy_hp <= 0
    stage_completed = False
    reward = 0
    chest_reward = 0
    last_enemy_attack_at = float(
        player_data["last_enemy_attack_at"]
    )

    if enemy_defeated:
        reward = calculate_gold_reward(stage)
        chest_reward = 1
        gold += reward
        chests += chest_reward
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
            chests = ?,
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
            chests,
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
            f"🏆 Победа! +{reward} золота и +1 сундук"
        )

    if stage_completed:
        message = (
            f"🚪 Открыт этап {stage}! +1 сундук"
        )

    return build_player_response(
        updated_player,
        attacked=True,
        enemy_defeated=enemy_defeated,
        stage_completed=stage_completed,
        reward=reward,
        chest_reward=chest_reward,
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


@app.post("/chest/upgrade")
def upgrade_chest(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        chest_level = max(1, int(current["chest_level"]))
        upgrade_cost = calculate_chest_upgrade_cost(chest_level)

        if upgrade_cost is None:
            connection.commit()
            return build_player_response(
                current,
                upgraded=False,
                max_level=True,
                message="Сундук уже максимального уровня",
            )

        gold = max(0, int(current["gold"]))
        if gold < upgrade_cost:
            connection.commit()
            return build_player_response(
                current,
                upgraded=False,
                not_enough_gold=True,
                missing_gold=upgrade_cost - gold,
                message=(
                    "Недостаточно золота для улучшения сундука"
                ),
            )

        new_level = chest_level + 1
        connection.execute(
            """
            UPDATE players
            SET chest_level = ?,
                gold = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                new_level,
                gold - upgrade_cost,
                int(time.time()),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    return build_player_response(
        updated,
        upgraded=True,
        previous_chest_level=chest_level,
        upgrade_cost_paid=upgrade_cost,
        message=f"⬆️ Сундук улучшен до {new_level} уровня",
    )


@app.post("/loot/open")
def open_loot(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        pending = public_pending_loot(current)

        if pending:
            connection.commit()
            return build_player_response(
                current,
                opened=False,
                pending_exists=True,
                loot=pending,
                message="Сначала решите судьбу найденного предмета",
            )

        chests = int(current["chests"])
        if chests <= 0:
            connection.commit()
            return build_player_response(
                current,
                opened=False,
                no_chests=True,
                message="Сундуков пока нет. Побеждайте врагов.",
            )

        loot = generate_loot(
            int(current["stage"]),
            int(current["chest_level"]),
        )
        connection.execute(
            """
            UPDATE players
            SET chests = ?,
                pending_loot_json = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                chests - 1,
                json.dumps(loot, ensure_ascii=False),
                int(time.time()),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    return build_player_response(
        updated,
        opened=True,
        loot=loot,
        message=f"Найден предмет: {loot['name']}",
    )


@app.post("/loot/equip")
def equip_loot(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        loot = public_pending_loot(current)
        if not loot:
            raise HTTPException(
                status_code=409,
                detail="Нет предмета для экипировки",
            )

        equipment = parse_json_object(current["equipment_json"])
        slot_key = str(loot["slot"])
        replaced_item = equipment.get(slot_key)
        replaced_reward = 0
        gold = int(current["gold"])

        if isinstance(replaced_item, dict):
            replaced_reward = max(
                0,
                int(replaced_item.get("sell_price", 0)),
            )
            gold += replaced_reward

        equipment[slot_key] = loot
        stats = calculate_equipment_stats(equipment)
        old_max_hp = max(1, int(current["hero_max_hp"]))
        hero_hp = max(0, int(current["hero_hp"]))
        max_hp_gain = stats["hero_max_hp"] - old_max_hp
        if max_hp_gain > 0:
            hero_hp += max_hp_gain
        hero_hp = min(hero_hp, stats["hero_max_hp"])

        connection.execute(
            """
            UPDATE players
            SET equipment_json = ?,
                pending_loot_json = '',
                gold = ?,
                power = ?,
                damage = ?,
                hero_max_hp = ?,
                hero_hp = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                json.dumps(equipment, ensure_ascii=False),
                gold,
                stats["power"],
                stats["damage"],
                stats["hero_max_hp"],
                hero_hp,
                int(time.time()),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    message = f"✅ Надето: {loot['name']}"
    if replaced_reward:
        message += f". Старый предмет продан за {replaced_reward}"

    return build_player_response(
        updated,
        equipped=True,
        item=loot,
        replaced_item=replaced_item,
        replaced_reward=replaced_reward,
        message=message,
    )


@app.post("/loot/sell")
def sell_loot(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(
        x_telegram_init_data
    )
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        loot = public_pending_loot(current)
        if not loot:
            raise HTTPException(
                status_code=409,
                detail="Нет предмета для продажи",
            )

        sell_price = max(0, int(loot.get("sell_price", 0)))
        gold = int(current["gold"]) + sell_price
        connection.execute(
            """
            UPDATE players
            SET pending_loot_json = '',
                gold = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                gold,
                int(time.time()),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    return build_player_response(
        updated,
        sold=True,
        item=loot,
        sell_price=sell_price,
        message=f"💰 Предмет продан за {sell_price} золота",
    )
