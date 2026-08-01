import bisect
import hashlib
import hmac
import json
import math
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
BASE_CRIT_CHANCE = 5.0
BASE_CRIT_DAMAGE = 150.0
ENEMIES_PER_STAGE = 10
MAX_STAGE = 1000
MAX_HERO_LEVEL = 100
MIN_ATTACK_INTERVAL = 0.1
ENEMY_ATTACK_SPEED = 0.5
MIN_ENEMY_ATTACK_INTERVAL = 0.5
STARTER_CHESTS = 3
MAX_CHEST_LEVEL = 25
CHEST_UPGRADE_BASE_COST = 300
CHEST_UPGRADE_COST_GROWTH = 1.48
OFFLINE_MAX_SECONDS = 4 * 60 * 60
OFFLINE_CHEST_INTERVAL = 20 * 60
MAX_SAFE_STAT = 9_000_000_000_000_000

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
    {
        "key": "ancient",
        "name": "Древний",
        "adjective": "Древний",
        "multiplier": 7.2,
        "sell_multiplier": 6.5,
    },
    {
        "key": "divine",
        "name": "Божественный",
        "adjective": "Божественный",
        "multiplier": 10.5,
        "sell_multiplier": 9.5,
    },
    {
        "key": "celestial",
        "name": "Небесный",
        "adjective": "Небесный",
        "multiplier": 15.0,
        "sell_multiplier": 13.5,
    },
)

LEVEL_STAGE_ANCHORS = (
    (1, 1),
    (26, 100),
    (31, 150),
    (37, 200),
    (49, 300),
    (61, 400),
    (73, 500),
    (86, 600),
    (100, 700),
)

app = FastAPI(title="Telegram Idle RPG API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://kuba092.github.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def clamp_int(value: int | float, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(round(value))))


def get_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
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


def calculate_enemy_hp(stage: int, boss: bool = False) -> int:
    stage = clamp_int(stage, 1, MAX_STAGE)
    normal_hp = BASE_ENEMY_HP * (1 + 0.14 * (stage - 1)) ** 2.2
    if boss:
        multiplier = 9.0 if stage % 10 == 0 else 6.0
        normal_hp *= multiplier
    return clamp_int(normal_hp, 1, MAX_SAFE_STAT)


def calculate_enemy_damage(stage: int, boss: bool = False) -> int:
    stage = clamp_int(stage, 1, MAX_STAGE)
    normal_damage = BASE_ENEMY_DAMAGE * (1 + 0.10 * (stage - 1)) ** 1.5
    if boss:
        multiplier = 2.2 if stage % 10 == 0 else 1.8
        normal_damage *= multiplier
    return clamp_int(normal_damage, 1, MAX_SAFE_STAT)


def calculate_exp_reward(stage: int, boss: bool = False) -> int:
    stage = clamp_int(stage, 1, MAX_STAGE)
    normal_reward = max(1, round(2.05 * stage ** 0.866))
    return normal_reward * (12 if boss else 1)


def stage_total_exp(stage: int) -> int:
    return (
        ENEMIES_PER_STAGE * calculate_exp_reward(stage)
        + calculate_exp_reward(stage, boss=True)
    )


CUMULATIVE_STAGE_EXP = [0]
for _stage in range(1, MAX_STAGE + 1):
    CUMULATIVE_STAGE_EXP.append(
        CUMULATIVE_STAGE_EXP[-1] + stage_total_exp(_stage)
    )


def target_stage_for_level(level: int) -> float:
    level = clamp_int(level, 1, MAX_HERO_LEVEL)
    for index in range(len(LEVEL_STAGE_ANCHORS) - 1):
        level_a, stage_a = LEVEL_STAGE_ANCHORS[index]
        level_b, stage_b = LEVEL_STAGE_ANCHORS[index + 1]
        if level_a <= level <= level_b:
            if level_b == level_a:
                return float(stage_b)
            ratio = (level - level_a) / (level_b - level_a)
            return stage_a + (stage_b - stage_a) * ratio
    return float(LEVEL_STAGE_ANCHORS[-1][1])


def cumulative_exp_at_stage(stage_value: float) -> int:
    stage_value = max(0.0, min(float(MAX_STAGE), stage_value))
    whole_stage = int(math.floor(stage_value))
    fraction = stage_value - whole_stage
    total = CUMULATIVE_STAGE_EXP[whole_stage]
    if fraction > 0 and whole_stage < MAX_STAGE:
        total += round(stage_total_exp(whole_stage + 1) * fraction)
    return total


LEVEL_TOTAL_EXP = [0] * (MAX_HERO_LEVEL + 1)
LEVEL_TOTAL_EXP[1] = 0
for _level in range(2, MAX_HERO_LEVEL + 1):
    threshold = cumulative_exp_at_stage(target_stage_for_level(_level))
    LEVEL_TOTAL_EXP[_level] = max(
        LEVEL_TOTAL_EXP[_level - 1] + 1,
        threshold,
    )


def level_from_total_exp(total_exp: int) -> int:
    total_exp = max(0, int(total_exp))
    return min(
        MAX_HERO_LEVEL,
        max(1, bisect.bisect_right(LEVEL_TOTAL_EXP, total_exp) - 1),
    )


def level_exp_details(level: int, total_exp: int) -> dict:
    level = clamp_int(level, 1, MAX_HERO_LEVEL)
    total_exp = max(0, int(total_exp))
    if level >= MAX_HERO_LEVEL:
        return {
            "current": 0,
            "required": 0,
            "remaining": 0,
            "progress": 1.0,
            "max_level": True,
        }
    start = LEVEL_TOTAL_EXP[level]
    end = LEVEL_TOTAL_EXP[level + 1]
    current = max(0, total_exp - start)
    required = max(1, end - start)
    return {
        "current": current,
        "required": required,
        "remaining": max(0, required - current),
        "progress": round(min(1.0, current / required), 4),
        "max_level": False,
    }


def hero_base_stats(level: int) -> dict:
    level = clamp_int(level, 1, MAX_HERO_LEVEL)
    return {
        "power": BASE_POWER + (level - 1) * 5,
        "damage": BASE_HERO_DAMAGE + (level - 1) * 2,
        "hero_max_hp": BASE_HERO_HP + (level - 1) * 12,
        "attack_speed": 1.0,
        "crit_chance": BASE_CRIT_CHANCE,
        "crit_damage": BASE_CRIT_DAMAGE + (level // 10) * 5,
    }


def calculate_equipment_stats(equipment: dict, level: int = 1) -> dict:
    base = hero_base_stats(level)
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
        "base": base,
        "equipment": {
            "power": power_bonus,
            "damage": damage_bonus,
            "hero_max_hp": hp_bonus,
        },
        "total": {
            "power": clamp_int(base["power"] + power_bonus, 1, MAX_SAFE_STAT),
            "damage": clamp_int(base["damage"] + damage_bonus, 1, MAX_SAFE_STAT),
            "hero_max_hp": clamp_int(
                base["hero_max_hp"] + hp_bonus,
                1,
                MAX_SAFE_STAT,
            ),
            "attack_speed": base["attack_speed"],
            "crit_chance": base["crit_chance"],
            "crit_damage": base["crit_damage"],
        },
    }


def sync_player_stats(
    connection: sqlite3.Connection,
    telegram_id: int,
) -> None:
    row = connection.execute(
        """
        SELECT equipment_json, hero_hp, hero_max_hp, level
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()
    if row is None:
        return
    equipment = parse_json_object(row["equipment_json"])
    stats = calculate_equipment_stats(equipment, int(row["level"]))
    total = stats["total"]
    current_max_hp = max(1, int(row["hero_max_hp"]))
    current_hp = max(0, int(row["hero_hp"]))
    max_hp_gain = total["hero_max_hp"] - current_max_hp
    if max_hp_gain > 0:
        current_hp += max_hp_gain
    current_hp = min(current_hp, total["hero_max_hp"])
    connection.execute(
        """
        UPDATE players
        SET damage = ?,
            power = ?,
            hero_max_hp = ?,
            hero_hp = ?,
            attack_speed = ?
        WHERE telegram_id = ?
        """,
        (
            total["damage"],
            total["power"],
            total["hero_max_hp"],
            current_hp,
            total["attack_speed"],
            telegram_id,
        ),
    )


def chest_upgrade_steps_required(chest_level: int) -> int:
    chest_level = clamp_int(chest_level, 1, MAX_CHEST_LEVEL)
    if chest_level >= MAX_CHEST_LEVEL:
        return 0
    if chest_level <= 3:
        return 1
    if chest_level <= 7:
        return 2
    if chest_level <= 12:
        return 3
    if chest_level <= 17:
        return 4
    if chest_level <= 21:
        return 5
    if chest_level <= 23:
        return 6
    return 8


def calculate_chest_upgrade_cost(chest_level: int) -> int | None:
    chest_level = clamp_int(chest_level, 1, MAX_CHEST_LEVEL)
    if chest_level >= MAX_CHEST_LEVEL:
        return None
    total_level_cost = max(
        1,
        round(
            CHEST_UPGRADE_BASE_COST
            * (CHEST_UPGRADE_COST_GROWTH ** (chest_level - 1))
        ),
    )
    steps = max(1, chest_upgrade_steps_required(chest_level))
    return max(1, math.ceil(total_level_cost / steps))


def chest_rarity_weights(chest_level: int) -> list[float]:
    chest_level = clamp_int(chest_level, 1, MAX_CHEST_LEVEL)
    if chest_level <= 3:
        return [72, 25, 3, 0, 0, 0, 0, 0, 0]
    if chest_level <= 7:
        return [55, 32, 11, 2, 0, 0, 0, 0, 0]
    if chest_level <= 12:
        return [38, 34, 21, 6, 1, 0, 0, 0, 0]
    if chest_level <= 16:
        return [25, 31, 28, 12, 3.5, 0.5, 0, 0, 0]
    if chest_level <= 19:
        return [15, 25, 31, 20, 7, 2, 0, 0, 0]
    if chest_level <= 22:
        return [9, 18, 28, 25, 13, 5, 2, 0, 0]
    if chest_level <= 24:
        return [5, 12, 22, 25, 18, 10, 5, 3, 0]
    return [3, 8, 18, 23, 20, 13, 8, 5, 2]


def chest_rarity_chances(chest_level: int) -> dict:
    weights = chest_rarity_weights(chest_level)
    total = sum(weights) or 1
    return {
        rarity["key"]: round(weight * 100 / total, 2)
        for rarity, weight in zip(RARITIES, weights)
    }


def choose_rarity(chest_level: int) -> dict:
    weights = chest_rarity_weights(chest_level)
    chosen_index = random.choices(
        list(range(len(RARITIES))),
        weights=weights,
        k=1,
    )[0]
    return RARITIES[chosen_index]


def generate_loot(stage: int, chest_level: int) -> dict:
    slot_key = random.choice(list(GEAR_SLOTS))
    slot = GEAR_SLOTS[slot_key]
    stage = clamp_int(stage, 1, MAX_STAGE)
    chest_level = clamp_int(chest_level, 1, MAX_CHEST_LEVEL)
    rarity = choose_rarity(chest_level)
    random_bonus = random.randint(0, max(2, stage // 20 + chest_level))
    base_roll = stage * 1.35 + chest_level * 2.5 + random_bonus
    chest_power_multiplier = 1 + (chest_level - 1) * 0.06
    item_power = clamp_int(
        base_roll * chest_power_multiplier * rarity["multiplier"],
        1,
        MAX_SAFE_STAT,
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
    sell_price = clamp_int(
        item_power
        * rarity["sell_multiplier"]
        * (2.5 + chest_level * 0.04),
        3,
        MAX_SAFE_STAT,
    )
    return {
        "id": secrets.token_hex(8),
        "slot": slot_key,
        "slot_name": slot["name"],
        "icon": slot["icon"],
        "name": f"{rarity['adjective']} {slot['item']}",
        "rarity": rarity["key"],
        "rarity_name": rarity["name"],
        "power": item_power,
        "damage": damage_bonus,
        "hp": hp_bonus,
        "sell_price": sell_price,
        "stage_found": stage,
        "chest_level_found": chest_level,
    }


def current_wave(player: dict) -> int:
    kills = clamp_int(player.get("kills_in_stage", 0), 0, ENEMIES_PER_STAGE)
    if (
        int(player.get("boss_active", 0))
        or int(player.get("boss_waiting", 0))
        or int(player.get("game_completed", 0))
        or kills >= ENEMIES_PER_STAGE
    ):
        return ENEMIES_PER_STAGE
    return kills + 1


def wave_progress(player: dict) -> int:
    stage = clamp_int(player.get("stage", 1), 1, MAX_STAGE)
    return stage * 10 + current_wave(player)


def stage_progress_label(player: dict) -> str:
    stage = clamp_int(player.get("stage", 1), 1, MAX_STAGE)
    if int(player.get("game_completed", 0)):
        return f"Этап {stage} • ПРОЙДЕНО"
    if int(player.get("boss_active", 0)) or int(player.get("boss_waiting", 0)):
        return f"Этап {stage} • БОСС"
    return f"Этап {stage} • {current_wave(player)}/10"


def boss_reward_chests(stage: int) -> int:
    return 5 if int(stage) % 10 == 0 else 3


def compare_loot(player: dict, loot: dict) -> dict:
    equipment = parse_json_object(player.get("equipment_json"))
    level = int(player.get("level", 1))
    before = calculate_equipment_stats(equipment, level)["total"]
    slot_key = str(loot["slot"])
    equipped_item = equipment.get(slot_key)
    after_equipment = dict(equipment)
    after_equipment[slot_key] = loot
    after = calculate_equipment_stats(after_equipment, level)["total"]
    result = {}
    for key, public_name in (
        ("power", "power"),
        ("damage", "damage"),
        ("hero_max_hp", "hp"),
    ):
        old_value = int(before[key])
        new_value = int(after[key])
        result[public_name] = {
            "old": old_value,
            "new": new_value,
            "delta": new_value - old_value,
        }
    is_empty_slot = not isinstance(equipped_item, dict)
    is_improvement = is_empty_slot or result["power"]["delta"] > 0
    return {
        **result,
        "is_improvement": is_improvement,
        "is_empty_slot": is_empty_slot,
        "equipped_item": equipped_item if isinstance(equipped_item, dict) else None,
        "equipped_item_name": (
            equipped_item.get("name")
            if isinstance(equipped_item, dict)
            else None
        ),
    }


def load_player(
    connection: sqlite3.Connection,
    telegram_id: int,
) -> dict:
    player = connection.execute(
        "SELECT * FROM players WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()
    if player is None:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    return dict(player)


def apply_offline_accrual(
    connection: sqlite3.Connection,
    player: dict,
    now: int,
) -> None:
    last_active = int(player.get("last_active_at", 0) or 0)
    if last_active <= 0:
        return
    elapsed = max(0, now - last_active)
    eligible = min(elapsed, OFFLINE_MAX_SECONDS)
    chest_count = eligible // OFFLINE_CHEST_INTERVAL
    if chest_count <= 0:
        return
    highest_stage = clamp_int(
        player.get("highest_stage", player.get("stage", 1)),
        1,
        MAX_STAGE,
    )
    experience = chest_count * calculate_exp_reward(highest_stage)
    connection.execute(
        """
        UPDATE players
        SET offline_pending_chests = offline_pending_chests + ?,
            offline_pending_exp = offline_pending_exp + ?
        WHERE telegram_id = ?
        """,
        (chest_count, experience, int(player["telegram_id"])),
    )


def get_or_create_player(user: dict, accrue_offline: bool = False) -> dict:
    telegram_id = int(user["id"])
    username = user.get("username", "")
    first_name = user.get("first_name", "Игрок")
    now = int(time.time())
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR IGNORE INTO players (
                telegram_id, username, first_name,
                updated_at, last_active_at, progress_reached_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (telegram_id, username, first_name, now, now, now),
        )
        player = load_player(connection, telegram_id)
        if accrue_offline:
            apply_offline_accrual(connection, player, now)
        connection.execute(
            """
            UPDATE players
            SET username = ?, first_name = ?,
                updated_at = ?, last_active_at = ?
            WHERE telegram_id = ?
            """,
            (username, first_name, now, now, telegram_id),
        )
        sync_player_stats(connection, telegram_id)
        connection.commit()
        return load_player(connection, telegram_id)
    finally:
        connection.close()


def public_equipment(player: dict) -> dict:
    stored = parse_json_object(player.get("equipment_json"))
    return {slot_key: stored.get(slot_key) for slot_key in GEAR_SLOTS}


def public_pending_loot(player: dict) -> dict | None:
    pending = parse_json_object(player.get("pending_loot_json"))
    return pending or None


def build_player_response(player: dict, **extra) -> dict:
    attack_speed = max(0.1, float(player.get("attack_speed", 1.0)))
    attack_interval = max(MIN_ATTACK_INTERVAL, 1 / attack_speed)
    boss_active = bool(int(player.get("boss_active", 0)))
    boss_waiting = bool(int(player.get("boss_waiting", 0)))
    game_completed = bool(int(player.get("game_completed", 0)))
    stage = clamp_int(player.get("stage", 1), 1, MAX_STAGE)
    level = clamp_int(player.get("level", 1), 1, MAX_HERO_LEVEL)
    total_exp = max(0, int(player.get("experience", 0)))
    equipment = public_equipment(player)
    stats = calculate_equipment_stats(equipment, level)
    chest_level = clamp_int(player.get("chest_level", 1), 1, MAX_CHEST_LEVEL)
    chest_step = max(0, int(player.get("chest_upgrade_step", 0)))
    steps_required = chest_upgrade_steps_required(chest_level)
    pending_loot = public_pending_loot(player)
    hidden_fields = {"equipment_json", "pending_loot_json"}
    public_player = {
        key: value
        for key, value in player.items()
        if key not in hidden_fields
    }
    response = {
        **public_player,
        "equipment": equipment,
        "pending_loot": pending_loot,
        "pending_loot_comparison": (
            compare_loot(player, pending_loot) if pending_loot else None
        ),
        "hero_stats": stats,
        "crit_chance": stats["total"]["crit_chance"],
        "crit_damage": stats["total"]["crit_damage"],
        "attack_interval": attack_interval,
        "enemy_damage": calculate_enemy_damage(stage, boss=boss_active),
        "enemy_attack_speed": ENEMY_ATTACK_SPEED,
        "enemy_attack_interval": max(
            MIN_ENEMY_ATTACK_INTERVAL,
            1 / ENEMY_ATTACK_SPEED,
        ),
        "enemy_is_boss": boss_active,
        "boss_active": boss_active,
        "boss_waiting": boss_waiting,
        "boss_button_visible": boss_waiting and not game_completed,
        "can_attack": (
            int(player.get("hero_hp", 0)) > 0
            and not boss_waiting
            and not game_completed
        ),
        "hero_alive": int(player.get("hero_hp", 0)) > 0,
        "game_completed": game_completed,
        "stage_max": MAX_STAGE,
        "wave": current_wave(player),
        "wave_progress": wave_progress(player),
        "stage_progress_label": stage_progress_label(player),
        "level_max": MAX_HERO_LEVEL,
        "level_exp": level_exp_details(level, total_exp),
        "chest_level": chest_level,
        "chest_max_level": MAX_CHEST_LEVEL,
        "chest_upgrade_step": chest_step,
        "chest_upgrade_steps_required": steps_required,
        "chest_upgrade_cost": calculate_chest_upgrade_cost(chest_level),
        "chest_rarity_chances": chest_rarity_chances(chest_level),
        "offline_reward": {
            "chests": max(0, int(player.get("offline_pending_chests", 0))),
            "experience": max(0, int(player.get("offline_pending_exp", 0))),
            "available": (
                int(player.get("offline_pending_chests", 0)) > 0
                or int(player.get("offline_pending_exp", 0)) > 0
            ),
            "max_hours": 4,
            "chest_interval_minutes": 20,
        },
        **extra,
    }
    return response


def validate_telegram_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(status_code=500, detail="BOT_TOKEN не настроен")
    parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed_data.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Нет подписи Telegram")
    try:
        auth_date = int(parsed_data.get("auth_date", "0"))
    except ValueError as error:
        raise HTTPException(
            status_code=401,
            detail="Некорректная дата Telegram",
        ) from error
    if time.time() - auth_date > 86400:
        raise HTTPException(status_code=401, detail="Данные Telegram устарели")
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed_data.items())
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
        raise HTTPException(status_code=401, detail="Неверная подпись Telegram")
    user_data = parsed_data.get("user")
    if not user_data:
        raise HTTPException(status_code=401, detail="Telegram не передал игрока")
    return json.loads(user_data)


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
        ("chest_upgrade_step", "INTEGER NOT NULL DEFAULT 0"),
        ("equipment_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("pending_loot_json", "TEXT NOT NULL DEFAULT ''"),
        ("experience", "INTEGER NOT NULL DEFAULT 0"),
        ("highest_stage", "INTEGER NOT NULL DEFAULT 1"),
        ("boss_active", "INTEGER NOT NULL DEFAULT 0"),
        ("boss_waiting", "INTEGER NOT NULL DEFAULT 0"),
        ("game_completed", "INTEGER NOT NULL DEFAULT 0"),
        ("total_bosses", "INTEGER NOT NULL DEFAULT 0"),
        ("last_active_at", "INTEGER NOT NULL DEFAULT 0"),
        ("offline_pending_chests", "INTEGER NOT NULL DEFAULT 0"),
        ("offline_pending_exp", "INTEGER NOT NULL DEFAULT 0"),
        ("progress_reached_at", "INTEGER NOT NULL DEFAULT 0"),
        ("auto_open_enabled", "INTEGER NOT NULL DEFAULT 0"),
    )
    for column_name, definition in columns:
        add_column_if_missing(connection, column_name, definition)
    now = int(time.time())
    connection.execute(
        """
        UPDATE players
        SET stage = CASE WHEN stage < 1 THEN 1 WHEN stage > ? THEN ? ELSE stage END,
            highest_stage = CASE
                WHEN highest_stage < stage THEN stage
                WHEN highest_stage > ? THEN ?
                ELSE highest_stage
            END,
            level = CASE WHEN level < 1 THEN 1 WHEN level > ? THEN ? ELSE level END,
            chest_level = CASE
                WHEN chest_level < 1 THEN 1
                WHEN chest_level > ? THEN ?
                ELSE chest_level
            END,
            kills_in_stage = CASE
                WHEN kills_in_stage < 0 THEN 0
                WHEN kills_in_stage > ? THEN ?
                ELSE kills_in_stage
            END,
            last_active_at = CASE WHEN last_active_at <= 0 THEN ? ELSE last_active_at END,
            progress_reached_at = CASE
                WHEN progress_reached_at <= 0 THEN ? ELSE progress_reached_at END
        """,
        (
            MAX_STAGE,
            MAX_STAGE,
            MAX_STAGE,
            MAX_STAGE,
            MAX_HERO_LEVEL,
            MAX_HERO_LEVEL,
            MAX_CHEST_LEVEL,
            MAX_CHEST_LEVEL,
            ENEMIES_PER_STAGE,
            ENEMIES_PER_STAGE,
            now,
            now,
        ),
    )
    rows = connection.execute("SELECT * FROM players").fetchall()
    for row in rows:
        player = dict(row)
        telegram_id = int(player["telegram_id"])
        experience = max(0, int(player.get("experience", 0)))
        calculated_level = level_from_total_exp(experience)
        stored_level = clamp_int(player.get("level", 1), 1, MAX_HERO_LEVEL)
        level = max(stored_level, calculated_level)
        connection.execute(
            "UPDATE players SET level = ? WHERE telegram_id = ?",
            (level, telegram_id),
        )
        sync_player_stats(connection, telegram_id)
        refreshed = load_player(connection, telegram_id)
        boss = bool(int(refreshed.get("boss_active", 0)))
        expected_max = calculate_enemy_hp(int(refreshed["stage"]), boss=boss)
        current_hp = max(0, int(refreshed.get("enemy_hp", expected_max)))
        current_max = max(1, int(refreshed.get("enemy_max_hp", expected_max)))
        ratio = min(1.0, current_hp / current_max)
        new_hp = max(1, round(expected_max * ratio)) if current_hp > 0 else 0
        connection.execute(
            """
            UPDATE players
            SET enemy_max_hp = ?, enemy_hp = ?
            WHERE telegram_id = ?
            """,
            (expected_max, new_hp, telegram_id),
        )
    connection.commit()
    connection.close()


@app.on_event("startup")
def startup_event() -> None:
    create_database()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/player")
def player(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user, accrue_offline=True)
    return build_player_response(player_data)


@app.post("/offline/claim")
def claim_offline_reward(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        pending_chests = max(0, int(current["offline_pending_chests"]))
        pending_exp = max(0, int(current["offline_pending_exp"]))
        if pending_chests <= 0 and pending_exp <= 0:
            connection.commit()
            return build_player_response(
                current,
                claimed=False,
                message="Офлайн-награда пока не накопилась",
            )
        total_exp = max(0, int(current["experience"])) + pending_exp
        new_level = level_from_total_exp(total_exp)
        connection.execute(
            """
            UPDATE players
            SET chests = chests + ?,
                experience = ?, level = ?,
                offline_pending_chests = 0,
                offline_pending_exp = 0,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (pending_chests, total_exp, new_level, int(time.time()), telegram_id),
        )
        sync_player_stats(connection, telegram_id)
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()
    return build_player_response(
        updated,
        claimed=True,
        claimed_chests=pending_chests,
        claimed_experience=pending_exp,
        message=f"🎁 Получено: {pending_chests} сундуков и {pending_exp} опыта",
    )


@app.post("/attack")
def attack(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        if int(current["hero_hp"]) <= 0:
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                hero_defeated=True,
                message="💀 Герой ожидает возрождения",
            )
        if int(current.get("game_completed", 0)):
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                game_completed=True,
                message="🏆 Все 1000 этапов пройдены",
            )
        if int(current.get("boss_waiting", 0)):
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                boss_waiting=True,
                message="Нажмите кнопку «Босс», чтобы начать реванш",
            )
        attack_speed = max(0.1, float(current["attack_speed"]))
        attack_interval = max(MIN_ATTACK_INTERVAL, 1 / attack_speed)
        elapsed = now - float(current["last_attack_at"])
        if current["last_attack_at"] > 0 and elapsed < attack_interval:
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                retry_after=attack_interval - elapsed,
                message="Атака ещё не готова",
            )
        stage = clamp_int(current["stage"], 1, MAX_STAGE)
        boss_active = bool(int(current.get("boss_active", 0)))
        base_damage = max(1, int(current["damage"]))
        stats = calculate_equipment_stats(
            parse_json_object(current["equipment_json"]),
            int(current["level"]),
        )["total"]
        critical = random.random() < float(stats["crit_chance"]) / 100
        dealt_damage = (
            max(1, round(base_damage * float(stats["crit_damage"]) / 100))
            if critical
            else base_damage
        )
        enemy_hp = int(current["enemy_hp"]) - dealt_damage
        enemy_max_hp = int(current["enemy_max_hp"])
        enemy_defeated = enemy_hp <= 0
        stage_completed = False
        boss_defeated = False
        chest_reward = 0
        experience_reward = 0
        kills = int(current["kills_in_stage"])
        total_kills = int(current["total_kills"])
        total_bosses = int(current.get("total_bosses", 0))
        chests = int(current["chests"])
        highest_stage = max(stage, int(current.get("highest_stage", stage)))
        boss_waiting = 0
        game_completed = int(current.get("game_completed", 0))
        old_progress = wave_progress(current)
        last_enemy_attack_at = float(current["last_enemy_attack_at"])
        if enemy_defeated:
            total_kills += 1
            experience_reward = calculate_exp_reward(stage, boss=boss_active)
            if boss_active:
                boss_defeated = True
                total_bosses += 1
                chest_reward = boss_reward_chests(stage)
                chests += chest_reward
                if stage >= MAX_STAGE:
                    game_completed = 1
                    kills = ENEMIES_PER_STAGE
                    enemy_hp = 0
                    enemy_max_hp = calculate_enemy_hp(stage, boss=True)
                    boss_active = False
                else:
                    stage += 1
                    highest_stage = max(highest_stage, stage)
                    kills = 0
                    boss_active = False
                    enemy_max_hp = calculate_enemy_hp(stage)
                    enemy_hp = enemy_max_hp
                    last_enemy_attack_at = now
                    stage_completed = True
            else:
                chest_reward = 1
                chests += 1
                kills += 1
                if kills >= ENEMIES_PER_STAGE:
                    kills = ENEMIES_PER_STAGE
                    boss_active = True
                    enemy_max_hp = calculate_enemy_hp(stage, boss=True)
                    enemy_hp = enemy_max_hp
                else:
                    enemy_max_hp = calculate_enemy_hp(stage)
                    enemy_hp = enemy_max_hp
                last_enemy_attack_at = now
        total_exp = max(0, int(current.get("experience", 0))) + experience_reward
        new_level = level_from_total_exp(total_exp)
        provisional = dict(current)
        provisional.update(
            {
                "stage": stage,
                "kills_in_stage": kills,
                "boss_active": int(boss_active),
                "boss_waiting": boss_waiting,
                "game_completed": game_completed,
            }
        )
        new_progress = wave_progress(provisional)
        progress_reached_at = int(current.get("progress_reached_at", 0))
        if new_progress > old_progress:
            progress_reached_at = int(now)
        connection.execute(
            """
            UPDATE players
            SET enemy_hp = ?, enemy_max_hp = ?,
                stage = ?, kills_in_stage = ?,
                total_kills = ?, total_bosses = ?,
                chests = ?, experience = ?, level = ?,
                highest_stage = ?, boss_active = ?,
                boss_waiting = ?, game_completed = ?,
                progress_reached_at = ?,
                last_attack_at = ?, last_enemy_attack_at = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                max(0, enemy_hp),
                enemy_max_hp,
                stage,
                kills,
                total_kills,
                total_bosses,
                chests,
                total_exp,
                new_level,
                highest_stage,
                int(boss_active),
                boss_waiting,
                game_completed,
                progress_reached_at,
                now,
                last_enemy_attack_at,
                int(now),
                telegram_id,
            ),
        )
        sync_player_stats(connection, telegram_id)
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()
    message = f"⚔️ Нанесено {dealt_damage} урона"
    if critical:
        message = f"💥 Критический удар: {dealt_damage}"
    if enemy_defeated and not boss_defeated:
        if bool(int(updated.get("boss_active", 0))):
            message = "⚔️ Волна пройдена! Появился БОСС"
        else:
            message = f"🏆 Волна пройдена! +{chest_reward} сундук"
    if boss_defeated:
        if bool(int(updated.get("game_completed", 0))):
            message = f"🏆 Финальный босс побеждён! +{chest_reward} сундуков"
        else:
            message = f"👑 Босс побеждён! +{chest_reward} сундуков"
    return build_player_response(
        updated,
        attacked=True,
        damage_dealt=dealt_damage,
        critical=critical,
        enemy_defeated=enemy_defeated,
        boss_defeated=boss_defeated,
        stage_completed=stage_completed,
        chest_reward=chest_reward,
        experience_reward=experience_reward,
        reward=0,
        message=message,
    )


@app.post("/enemy-attack")
def enemy_attack(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        if int(current["hero_hp"]) <= 0:
            connection.commit()
            return build_player_response(
                current,
                enemy_attacked=False,
                hero_defeated=True,
                message="💀 Герой повержен",
            )
        if (
            int(current.get("boss_waiting", 0))
            or int(current.get("game_completed", 0))
        ):
            connection.commit()
            return build_player_response(
                current,
                enemy_attacked=False,
                message="Враг сейчас не атакует",
            )
        enemy_attack_interval = max(
            MIN_ENEMY_ATTACK_INTERVAL,
            1 / ENEMY_ATTACK_SPEED,
        )
        elapsed = now - float(current["last_enemy_attack_at"])
        if (
            current["last_enemy_attack_at"] > 0
            and elapsed < enemy_attack_interval
        ):
            connection.commit()
            return build_player_response(
                current,
                enemy_attacked=False,
                retry_after=enemy_attack_interval - elapsed,
                message="Атака врага ещё не готова",
            )
        stage = int(current["stage"])
        boss_active = bool(int(current.get("boss_active", 0)))
        received_damage = calculate_enemy_damage(stage, boss=boss_active)
        hero_hp = max(0, int(current["hero_hp"]) - received_damage)
        hero_defeated = hero_hp <= 0
        defeats = int(current["defeats"]) + (1 if hero_defeated else 0)
        boss_waiting = int(current.get("boss_waiting", 0))
        new_boss_active = int(boss_active)
        enemy_hp = int(current["enemy_hp"])
        enemy_max_hp = int(current["enemy_max_hp"])
        if hero_defeated and boss_active:
            new_boss_active = 0
            boss_waiting = 1
            enemy_max_hp = calculate_enemy_hp(stage, boss=True)
            enemy_hp = enemy_max_hp
        connection.execute(
            """
            UPDATE players
            SET hero_hp = ?, defeats = ?,
                boss_active = ?, boss_waiting = ?,
                enemy_hp = ?, enemy_max_hp = ?,
                last_enemy_attack_at = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                hero_hp,
                defeats,
                new_boss_active,
                boss_waiting,
                enemy_hp,
                enemy_max_hp,
                now,
                int(now),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()
    message = f"🩸 Враг нанёс {received_damage} урона"
    if hero_defeated and boss_active:
        message = "💀 Босс победил. После возрождения нажмите «Босс»"
    elif hero_defeated:
        message = "💀 Герой повержен"
    return build_player_response(
        updated,
        enemy_attacked=True,
        hero_defeated=hero_defeated,
        received_damage=received_damage,
        message=message,
    )


@app.post("/respawn")
def respawn(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        if int(current["hero_hp"]) > 0:
            connection.commit()
            return build_player_response(
                current,
                respawned=False,
                message="Герой уже в бою",
            )
        stage = int(current["stage"])
        boss_active = bool(int(current.get("boss_active", 0)))
        boss_waiting = bool(int(current.get("boss_waiting", 0)))
        enemy_max_hp = calculate_enemy_hp(
            stage,
            boss=boss_active or boss_waiting,
        )
        connection.execute(
            """
            UPDATE players
            SET hero_hp = hero_max_hp,
                enemy_hp = ?, enemy_max_hp = ?,
                last_attack_at = ?, last_enemy_attack_at = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                enemy_max_hp,
                enemy_max_hp,
                now,
                now,
                int(now),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()
    message = "✨ Герой возродился"
    if bool(int(updated.get("boss_waiting", 0))):
        message += ". Нажмите «Босс» для реванша"
    return build_player_response(updated, respawned=True, message=message)


@app.post("/boss/start")
def start_boss(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        if int(current["hero_hp"]) <= 0:
            connection.commit()
            return build_player_response(
                current,
                boss_started=False,
                message="Сначала возродите героя",
            )
        if not int(current.get("boss_waiting", 0)):
            connection.commit()
            return build_player_response(
                current,
                boss_started=False,
                message="Босс сейчас недоступен",
            )
        stage = int(current["stage"])
        boss_hp = calculate_enemy_hp(stage, boss=True)
        connection.execute(
            """
            UPDATE players
            SET boss_waiting = 0, boss_active = 1,
                enemy_hp = ?, enemy_max_hp = ?,
                last_attack_at = ?, last_enemy_attack_at = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (boss_hp, boss_hp, now, now, int(now), telegram_id),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()
    return build_player_response(
        updated,
        boss_started=True,
        message="👑 Реванш с боссом начался",
    )


@app.post("/chest/upgrade")
def upgrade_chest(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        chest_level = clamp_int(current["chest_level"], 1, MAX_CHEST_LEVEL)
        current_step = max(0, int(current.get("chest_upgrade_step", 0)))
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
                message="Недостаточно золота для улучшения сундука",
            )
        steps_required = chest_upgrade_steps_required(chest_level)
        next_step = current_step + 1
        new_level = chest_level
        level_completed = next_step >= steps_required
        if level_completed:
            new_level = min(MAX_CHEST_LEVEL, chest_level + 1)
            next_step = 0
        connection.execute(
            """
            UPDATE players
            SET chest_level = ?, chest_upgrade_step = ?,
                gold = ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                new_level,
                next_step,
                gold - upgrade_cost,
                int(time.time()),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()
    if level_completed:
        message = f"⬆️ Сундук улучшен до {new_level} уровня"
    else:
        message = f"🔨 Улучшение {next_step}/{steps_required}"
    return build_player_response(
        updated,
        upgraded=True,
        level_completed=level_completed,
        previous_chest_level=chest_level,
        upgrade_cost_paid=upgrade_cost,
        message=message,
    )


def open_loot_transaction(
    connection: sqlite3.Connection,
    current: dict,
    auto_mode: bool,
) -> tuple[dict, dict]:
    telegram_id = int(current["telegram_id"])
    pending = public_pending_loot(current)
    if pending:
        return current, {
            "opened": False,
            "pending_exists": True,
            "loot": pending,
            "comparison": compare_loot(current, pending),
            "message": "Сначала решите судьбу найденного предмета",
        }
    chests = int(current["chests"])
    if chests <= 0:
        return current, {
            "opened": False,
            "no_chests": True,
            "message": "Сундуков пока нет",
        }
    loot = generate_loot(
        int(current.get("highest_stage", current["stage"])),
        int(current["chest_level"]),
    )
    comparison = compare_loot(current, loot)
    now = int(time.time())
    if auto_mode and not comparison["is_improvement"]:
        sell_price = max(0, int(loot.get("sell_price", 0)))
        connection.execute(
            """
            UPDATE players
            SET chests = chests - 1,
                gold = gold + ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (sell_price, now, telegram_id),
        )
        updated = load_player(connection, telegram_id)
        return updated, {
            "opened": True,
            "auto_sold": True,
            "paused": False,
            "loot": loot,
            "comparison": comparison,
            "sell_price": sell_price,
            "message": f"💰 {loot['name']} продан за {sell_price}",
        }
    connection.execute(
        """
        UPDATE players
        SET chests = chests - 1,
            pending_loot_json = ?, updated_at = ?
        WHERE telegram_id = ?
        """,
        (json.dumps(loot, ensure_ascii=False), now, telegram_id),
    )
    updated = load_player(connection, telegram_id)
    return updated, {
        "opened": True,
        "auto_sold": False,
        "paused": auto_mode,
        "loot": loot,
        "comparison": comparison,
        "message": f"Найден предмет: {loot['name']}",
    }


@app.post("/loot/open")
def open_loot(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        updated, result = open_loot_transaction(connection, current, auto_mode=False)
        connection.commit()
    finally:
        connection.close()
    return build_player_response(updated, **result)


@app.post("/loot/auto/open")
def auto_open_loot(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        if not int(current.get("auto_open_enabled", 0)):
            connection.commit()
            return build_player_response(
                current,
                opened=False,
                auto_disabled=True,
                message="Автооткрытие выключено",
            )
        updated, result = open_loot_transaction(connection, current, auto_mode=True)
        connection.commit()
    finally:
        connection.close()
    return build_player_response(updated, **result)


@app.post("/loot/auto/enable")
def enable_auto_open(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    connection.execute(
        "UPDATE players SET auto_open_enabled = 1, updated_at = ? WHERE telegram_id = ?",
        (int(time.time()), telegram_id),
    )
    connection.commit()
    updated = load_player(connection, telegram_id)
    connection.close()
    return build_player_response(updated, auto_enabled=True, message="Авто включено")


@app.post("/loot/auto/disable")
def disable_auto_open(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    connection.execute(
        "UPDATE players SET auto_open_enabled = 0, updated_at = ? WHERE telegram_id = ?",
        (int(time.time()), telegram_id),
    )
    connection.commit()
    updated = load_player(connection, telegram_id)
    connection.close()
    return build_player_response(updated, auto_enabled=False, message="Авто выключено")


@app.post("/loot/equip")
def equip_loot(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        loot = public_pending_loot(current)
        if not loot:
            raise HTTPException(status_code=409, detail="Нет предмета для экипировки")
        comparison = compare_loot(current, loot)
        equipment = parse_json_object(current["equipment_json"])
        slot_key = str(loot["slot"])
        replaced_item = equipment.get(slot_key)
        replaced_reward = 0
        gold = int(current["gold"])
        if isinstance(replaced_item, dict):
            replaced_reward = max(0, int(replaced_item.get("sell_price", 0)))
            gold += replaced_reward
        equipment[slot_key] = loot
        old_max_hp = max(1, int(current["hero_max_hp"]))
        old_hp = max(0, int(current["hero_hp"]))
        stats = calculate_equipment_stats(equipment, int(current["level"]))["total"]
        hp_gain = stats["hero_max_hp"] - old_max_hp
        if hp_gain > 0:
            old_hp += hp_gain
        hero_hp = min(old_hp, stats["hero_max_hp"])
        connection.execute(
            """
            UPDATE players
            SET equipment_json = ?, pending_loot_json = '',
                gold = ?, power = ?, damage = ?,
                hero_max_hp = ?, hero_hp = ?,
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
        comparison=comparison,
        replaced_item=replaced_item,
        replaced_reward=replaced_reward,
        message=message,
    )


@app.post("/loot/sell")
def sell_loot(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        loot = public_pending_loot(current)
        if not loot:
            raise HTTPException(status_code=409, detail="Нет предмета для продажи")
        sell_price = max(0, int(loot.get("sell_price", 0)))
        connection.execute(
            """
            UPDATE players
            SET pending_loot_json = '',
                gold = gold + ?, updated_at = ?
            WHERE telegram_id = ?
            """,
            (sell_price, int(time.time()), telegram_id),
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


@app.get("/leaderboard")
def leaderboard(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    current_player = get_or_create_player(user)
    telegram_id = int(current_player["telegram_id"])
    connection = get_database()
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM players").fetchall()]
    finally:
        connection.close()
    def sort_key(item: dict) -> tuple:
        return (
            -wave_progress(item),
            -int(item.get("power", 0)),
            int(item.get("progress_reached_at", 0) or 0),
            int(item["telegram_id"]),
        )
    rows.sort(key=sort_key)
    entries = []
    own_entry = None
    for index, row in enumerate(rows, start=1):
        username = str(row.get("username") or "").strip()
        display_name = f"@{username}" if username else str(row.get("first_name") or "Игрок")
        entry = {
            "rank": index,
            "telegram_id": int(row["telegram_id"]),
            "name": display_name,
            "wave": wave_progress(row),
            "stage": int(row["stage"]),
            "stage_label": stage_progress_label(row),
            "power": int(row.get("power", 0)),
            "is_me": int(row["telegram_id"]) == telegram_id,
        }
        if index <= 100:
            entries.append(entry)
        if entry["is_me"]:
            own_entry = entry
    return {
        "entries": entries,
        "me": own_entry,
        "total_players": len(rows),
        "ranking_rule": "Волна → БМ → кто раньше достиг результата",
    }
