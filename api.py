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

from combat_effects import (
    BattleStateStore,
    SKILL_EFFECTS,
    CombatContext,
    CombatEffectEngine,
    CombatEvent,
    active_companion_sources,
    public_active_effects,
)


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
SPORE_STRIKE_UNLOCK_LEVEL = 5
SPORE_STRIKE_DAMAGE_MULTIPLIER = SKILL_EFFECTS["spore_strike"]["damage_multiplier"]
SPORE_STRIKE_COOLDOWN_SECONDS = SKILL_EFFECTS["spore_strike"]["cooldown_seconds"]
MUSHROOM_SHIELD_UNLOCK_LEVEL = 20
MUSHROOM_SHIELD_HP_RATIO = SKILL_EFFECTS["mushroom_shield"]["hp_ratio"]
MUSHROOM_SHIELD_COOLDOWN_SECONDS = SKILL_EFFECTS["mushroom_shield"]["cooldown_seconds"]
MUSHROOM_SHIELD_AUTO_HP_RATIO = 0.60
POISON_CLOUD_UNLOCK_LEVEL = 40
POISON_CLOUD_DAMAGE_MULTIPLIER = SKILL_EFFECTS["poison_cloud"]["damage_multiplier"]
POISON_CLOUD_DURATION_SECONDS = SKILL_EFFECTS["poison_cloud"]["duration_seconds"]
POISON_CLOUD_TICK_SECONDS = SKILL_EFFECTS["poison_cloud"]["tick_seconds"]
POISON_CLOUD_COOLDOWN_SECONDS = SKILL_EFFECTS["poison_cloud"]["cooldown_seconds"]

COMBAT_EFFECT_ENGINE = CombatEffectEngine()
BATTLE_STATES = BattleStateStore()


def battle_identity(player: dict) -> tuple:
    """Identity of the currently spawned campaign enemy (HP is deliberately excluded)."""
    return (
        int(player.get("stage", 1)),
        int(player.get("kills_in_stage", 0)),
        bool(int(player.get("boss_active", 0))),
        int(player.get("enemy_max_hp", 0)),
    )


def public_battle_identity(player: dict) -> str:
    return "|".join(str(int(value)) for value in battle_identity(player))


def owl_is_active(player: dict) -> bool:
    return any(
        effect[0].effect_id == "mushroom_owl"
        for effect in active_companion_sources(
            normalize_companion_slots(
                player.get("companion_slots_json"),
                normalize_companion_collection(player.get("companions_collection_json")),
            ),
            normalize_companion_collection(player.get("companions_collection_json")),
            sum(int(player.get("level", 1)) >= value for value in COMPANION_SLOT_UNLOCK_LEVELS),
        )
    )


def public_battle_effects(player: dict, now: float | None = None) -> dict:
    current_time = time.time() if now is None else now
    state = BATTLE_STATES.peek(int(player.get("telegram_id", 0)), battle_identity(player), current_time)
    owl = {
        effect.source_id: min(5, max(0, effect.stack_count - 1))
        for effect in (state.temporary_effects if state else [])
        if effect.effect_id == "mushroom_owl_repeat"
    }
    poison_stacks = state.stacks("poison_completion", "poison_cloud") if state else 0
    mitigation_remaining = BATTLE_STATES.mitigation_remaining(state, current_time)
    return {
        "owl_repeat_stacks": owl,
        "owl_repeat_bonus": {skill_id: round(stacks * .10, 4) for skill_id, stacks in owl.items()},
        "poison_completion_stacks": poison_stacks,
        "poison_stack_bonus": round(poison_stacks * .10, 4),
        "shield_mitigation_active": mitigation_remaining > 0,
        "shield_mitigation_remaining": round(mitigation_remaining, 3),
    }


def stage_sequence_state(player: dict, **overrides) -> dict:
    """Public, server-owned snapshot of HP and campaign-wave progression.

    Skill cooldowns intentionally reset when a defeated enemy is replaced.  Only
    persistent hero HP and permanent stats cross that boundary; shields, poison,
    Owl repeats, poison completions and mitigation are battle-local.
    """
    effects = calculate_companion_effects(player)
    max_hp = max(1, round(int(player.get("hero_max_hp", 1)) * effects["hp_multiplier"]))
    current_hp = max(0, min(max_hp, round(int(player.get("hero_hp", 0)) * effects["hp_multiplier"])))
    kills = clamp_int(player.get("kills_in_stage", 0), 0, ENEMIES_PER_STAGE)
    state = {
        "current_hp": current_hp,
        "max_hp": max_hp,
        "hp_percent": round(current_hp / max_hp * 100, 2),
        "hp_before_healing": current_hp,
        "hp_after_healing": current_hp,
        "hp_carried_to_next_battle": current_hp,
        "companion_healing": 0,
        "healing_overflow": 0,
        "kills_in_stage": kills,
        "enemies_remaining_before_boss": max(0, ENEMIES_PER_STAGE - kills),
        "boss_active": bool(int(player.get("boss_active", 0))),
        "temporary_effects_cleared": False,
        "cooldowns_reset_between_battles": False,
        "battle_identity": public_battle_identity(player),
    }
    state.update(overrides)
    return state

# COMPANION_SYSTEM_CONSTANTS_V1
COMPANION_SYSTEM_UNLOCK_LEVEL = 10
COMPANION_SLOT_UNLOCK_LEVELS = (10, 25, 50)
COMPANION_MAX_LEVEL = 20
COMPANION_SUMMON_MAX_LEVEL = 10
COMPANION_SUMMON_COST = 1

# COMPANION_SUMMON_LOGIC_V1
COMPANION_SUMMON_LEVEL_THRESHOLDS = {
    1: 0,
    2: 20,
    3: 60,
    4: 150,
    5: 350,
    6: 700,
    7: 1300,
    8: 2200,
    9: 3500,
    10: 5000,
}

COMPANION_SUMMON_RARITY_WEIGHTS = {
    1: {
        "common": 8490,
        "rare": 1350,
        "epic": 150,
        "legendary": 10,
    },
    2: {
        "common": 8220,
        "rare": 1550,
        "epic": 210,
        "legendary": 20,
    },
    3: {
        "common": 7900,
        "rare": 1800,
        "epic": 270,
        "legendary": 30,
    },
    4: {
        "common": 7550,
        "rare": 2080,
        "epic": 330,
        "legendary": 40,
    },
    5: {
        "common": 7150,
        "rare": 2400,
        "epic": 400,
        "legendary": 50,
    },
    6: {
        "common": 6700,
        "rare": 2700,
        "epic": 530,
        "legendary": 70,
    },
    7: {
        "common": 6200,
        "rare": 3000,
        "epic": 700,
        "legendary": 100,
    },
    8: {
        "common": 5600,
        "rare": 3300,
        "epic": 950,
        "legendary": 150,
    },
    9: {
        "common": 4900,
        "rare": 3600,
        "epic": 1250,
        "legendary": 250,
    },
    10: {
        "common": 4200,
        "rare": 3800,
        "epic": 1650,
        "legendary": 350,
    },
}

COMPANION_DUPLICATE_FRAGMENTS = {
    "common": 1,
    "rare": 2,
    "epic": 4,
    "legendary": 8,
}

COMPANION_RARITY_NAMES = {
    "common": "Обычный",
    "rare": "Редкий",
    "epic": "Эпический",
    "legendary": "Легендарный",
}

COMPANION_CATALOG = {
    "forest_sprite": {
        "name": "Лесной дух",
        "icon": "🌿",
        "rarity": "common",
        "implemented": True,
        "description": "Увеличивает итоговый урон героя на 1,3% за уровень.",
    },
    "baby_slime": {
        "name": "Маленький слизень",
        "icon": "🟢",
        "rarity": "common",
        "implemented": True,
        "description": "Увеличивает максимальный HP героя на 1,5% за уровень.",
    },
    "spore_beetle": {
        "name": "Споровый жук",
        "icon": "🪲",
        "rarity": "rare",
        "implemented": True,
        "description": "Добавляет к обычной атаке 2,8% базового урона за уровень.",
    },
    "mushroom_owl": {
        "name": "Грибная сова",
        "icon": "🦉",
        "rarity": "rare",
        "implemented": True,
        "description": "Снижает перезарядку навыков на 1,5% за уровень (до 30%).",
    },
    "thorn_wolf": {
        "name": "Шипастый волк",
        "icon": "🐺",
        "rarity": "epic",
        "implemented": True,
        "description": "Даёт +0,75 п.п. к шансу крита и +1% к критическому урону за уровень.",
    },
    "ancient_entling": {
        "name": "Древний энтёнок",
        "icon": "🌳",
        "rarity": "legendary",
        "implemented": True,
        "description": "Лечит на 0,6% максимального HP за уровень после победы (вдвое больше после босса).",
    },
}

COMPANION_DAMAGE_PER_LEVEL = 0.013
COMPANION_HP_PER_LEVEL = 0.015
COMPANION_EXTRA_ATTACK_DAMAGE_PER_LEVEL = 0.028
COMPANION_SKILL_COOLDOWN_REDUCTION_PER_LEVEL = 0.015
COMPANION_SKILL_COOLDOWN_MAX_REDUCTION = 0.30
COMPANION_CRIT_CHANCE_PER_LEVEL = 0.75
COMPANION_CRIT_DAMAGE_PER_LEVEL = 1.0
COMPANION_HEALING_PER_LEVEL = 0.006

SKILL_SYSTEM_UNLOCK_LEVEL = 5
SKILL_SLOT_UNLOCK_LEVELS = (5, 15, 30)
STARTER_SKILL_IDS = (
    "spore_strike",
    "mushroom_shield",
    "poison_cloud",
)
STARTER_SKILL_SCROLLS = 0
SKILL_POWER_PER_LEVEL = 0.05
SKILL_MAX_LEVEL = 20
SKILL_SUMMON_MAX_LEVEL = 10
SKILL_SUMMON_COST = 1

# Накопительное количество открытых свитков.
SKILL_SUMMON_LEVEL_THRESHOLDS = {
    1: 0,
    2: 20,
    3: 60,
    4: 150,
    5: 350,
    6: 700,
    7: 1300,
    8: 2200,
    9: 3500,
    10: 5000,
}

# Вероятности указаны в сотых долях процента.
SKILL_SUMMON_RARITY_WEIGHTS = {
    1: {
        "common": 8490,
        "rare": 1350,
        "epic": 150,
        "legendary": 10,
    },
    2: {
        "common": 8220,
        "rare": 1550,
        "epic": 210,
        "legendary": 20,
    },
    3: {
        "common": 7900,
        "rare": 1800,
        "epic": 270,
        "legendary": 30,
    },
    4: {
        "common": 7550,
        "rare": 2080,
        "epic": 330,
        "legendary": 40,
    },
    5: {
        "common": 7150,
        "rare": 2400,
        "epic": 400,
        "legendary": 50,
    },
    6: {
        "common": 6700,
        "rare": 2700,
        "epic": 530,
        "legendary": 70,
    },
    7: {
        "common": 6200,
        "rare": 3000,
        "epic": 700,
        "legendary": 100,
    },
    8: {
        "common": 5600,
        "rare": 3300,
        "epic": 950,
        "legendary": 150,
    },
    9: {
        "common": 4900,
        "rare": 3600,
        "epic": 1250,
        "legendary": 250,
    },
    10: {
        "common": 4200,
        "rare": 3800,
        "epic": 1650,
        "legendary": 350,
    },
}

SKILL_DUPLICATE_FRAGMENTS = {
    "common": 1,
    "rare": 2,
    "epic": 4,
    "legendary": 8,
}

SKILL_RARITY_NAMES = {
    "common": "Обычный",
    "rare": "Редкий",
    "epic": "Эпический",
    "legendary": "Легендарный",
}

SKILL_CATALOG = {
    "spore_strike": {
        "name": "Spore Strike",
        "icon": "🍄",
        "rarity": "common",
        "implemented": True,
        "description": "Наносит усиленный урон врагу.",
    },
    "thorn_burst": {
        "name": "Thorn Burst",
        "icon": "🌵",
        "rarity": "common",
        "implemented": False,
        "description": "Выпускает во врага залп острых шипов.",
    },
    "healing_dew": {
        "name": "Healing Dew",
        "icon": "💧",
        "rarity": "common",
        "implemented": False,
        "description": "Восстанавливает часть здоровья героя.",
    },
    "swift_cap": {
        "name": "Swift Cap",
        "icon": "💨",
        "rarity": "common",
        "implemented": False,
        "description": "Временно увеличивает скорость атаки.",
    },
    "poison_cloud": {
        "name": "Poison Cloud",
        "icon": "☁️",
        "rarity": "rare",
        "implemented": True,
        "description": "Наносит периодический урон ядом.",
    },
    "mushroom_shield": {
        "name": "Mushroom Shield",
        "icon": "🛡️",
        "rarity": "rare",
        "implemented": True,
        "description": "Создаёт щит, поглощающий урон.",
    },
    "root_snare": {
        "name": "Root Snare",
        "icon": "🌿",
        "rarity": "rare",
        "implemented": False,
        "description": "Замедляет атаки противника.",
    },
    "meteor_spores": {
        "name": "Meteor Spores",
        "icon": "☄️",
        "rarity": "epic",
        "implemented": False,
        "description": "Обрушивает на врага поток огненных спор.",
    },
    "phantom_clone": {
        "name": "Phantom Clone",
        "icon": "👻",
        "rarity": "epic",
        "implemented": False,
        "description": "Создаёт временную копию героя.",
    },
    "life_bloom": {
        "name": "Life Bloom",
        "icon": "🌺",
        "rarity": "epic",
        "implemented": False,
        "description": "Лечит героя и усиливает восстановление.",
    },
    "ancient_awakening": {
        "name": "Ancient Awakening",
        "icon": "✨",
        "rarity": "legendary",
        "implemented": False,
        "description": "Ненадолго значительно усиливает героя.",
    },
    "void_mycelium": {
        "name": "Void Mycelium",
        "icon": "🌌",
        "rarity": "legendary",
        "implemented": False,
        "description": "Наносит мощный урон силой пустоты.",
    },
}

DAILY_QUEST_DEFINITIONS = {
    "kill_enemies": {
        "name": "Охотник",
        "description": "Победите 30 обычных врагов",
        "icon": "⚔️",
        "counter": "daily_kills",
        "target": 30,
        "scrolls": 1,
    },
    "kill_bosses": {
        "name": "Победитель боссов",
        "description": "Победите 3 боссов",
        "icon": "👑",
        "counter": "daily_bosses",
        "target": 3,
        "scrolls": 2,
    },
    "open_chests": {
        "name": "Искатель сокровищ",
        "description": "Откройте 10 сундуков",
        "icon": "🧰",
        "counter": "daily_chests_opened",
        "target": 10,
        "scrolls": 1,
    },
}

DAILY_ALL_QUESTS_REWARD_SCROLLS = 3

# COMPANION_DAILY_REWARDS_V1
DAILY_ALL_QUESTS_REWARD_COMPANION_SCROLLS = 3

DAILY_ALL_QUESTS_ID = "daily_complete"

ENEMY_ATTACK_SPEED = 0.5
MIN_ENEMY_ATTACK_INTERVAL = 0.5
STARTER_CHESTS = 3
MAX_CHEST_LEVEL = 25
CHEST_UPGRADE_BASE_COST = 500
CHEST_UPGRADE_COST_GROWTH = 1.72
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


def calculate_hero_attack_damage(
    player: dict,
    damage_multiplier: float = 1.0,
) -> tuple[int, bool]:
    base_damage = max(1, int(player["damage"]))
    stats = calculate_equipment_stats(
        parse_json_object(player["equipment_json"]),
        int(player["level"]),
    )["total"]
    companion_effects = calculate_companion_effects(player)
    crit_chance = min(
        100.0,
        float(stats["crit_chance"])
        + float(companion_effects["crit_chance_bonus"]),
    )
    critical = random.random() < crit_chance / 100
    damage = base_damage * max(0.0, float(damage_multiplier))
    if critical:
        damage *= (
            float(stats["crit_damage"])
            + float(companion_effects["crit_damage_bonus"])
        ) / 100
    return max(1, round(damage)), critical


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


TOTAL_CHESTS_TO_MAX_LEVEL = 100_000
AVERAGE_CHEST_EXP_TARGET = 30
TOTAL_EXP_TO_MAX_LEVEL = (
    TOTAL_CHESTS_TO_MAX_LEVEL * AVERAGE_CHEST_EXP_TARGET
)
LEVEL_EXP_CURVE_POWER = 2.2


def chest_exp_reward(chest_level: int) -> int:
    """Опыт начисляется только за открытие сундука."""
    chest_level = clamp_int(chest_level, 1, MAX_CHEST_LEVEL)
    progress = (chest_level - 1) / max(1, MAX_CHEST_LEVEL - 1)
    return max(1, round(10 + 40 * progress))


LEVEL_TOTAL_EXP = [0] * (MAX_HERO_LEVEL + 1)
LEVEL_TOTAL_EXP[1] = 0

for _level in range(2, MAX_HERO_LEVEL + 1):
    progress = (_level - 1) / max(1, MAX_HERO_LEVEL - 1)
    threshold = round(
        TOTAL_EXP_TO_MAX_LEVEL
        * (progress ** LEVEL_EXP_CURVE_POWER)
    )
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
        return [79, 20, 1, 0, 0, 0, 0, 0, 0]

    if chest_level <= 7:
        return [63, 29, 7, 0.9, 0.1, 0, 0, 0, 0]

    if chest_level <= 12:
        return [48, 31, 16, 4.2, 0.7, 0.1, 0, 0, 0]

    if chest_level <= 16:
        return [38, 30, 22, 8, 1.7, 0.28, 0.02, 0, 0]

    if chest_level <= 19:
        return [30, 29, 25, 12, 3.2, 0.7, 0.1, 0, 0]

    if chest_level <= 22:
        return [
            23.75,
            28,
            28,
            15.5,
            3.2,
            1.2,
            0.3,
            0.05,
            0,
        ]

    if chest_level == 23:
        return [
            20.475,
            27.5,
            29.5,
            17.75,
            2.8,
            1.5,
            0.4,
            0.07,
            0.005,
        ]

    if chest_level == 24:
        return [
            18.77,
            26.5,
            30,
            19.02,
            3,
            2,
            0.6,
            0.1,
            0.01,
        ]

    return [
        17.015,
        25.5,
        29.5,
        21.315,
        3.2,
        2.5,
        0.8,
        0.15,
        0.02,
    ]






def chest_rarity_chances(chest_level: int) -> dict:
    weights = chest_rarity_weights(chest_level)
    total = sum(weights) or 1
    return {
        rarity["key"]: round(weight * 100 / total, 3)
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
    return 10 if int(stage) % 10 == 0 else 6


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
    experience = 0
    connection.execute(
        """
        UPDATE players
        SET offline_pending_chests = offline_pending_chests + ?,
            offline_pending_exp = offline_pending_exp + ?
        WHERE telegram_id = ?
        """,
        (chest_count, experience, int(player["telegram_id"])),
    )


# COMPANION_PLAYER_DATA_V1
def default_companion_collection() -> dict:
    return {}


def default_companion_slots() -> list[str | None]:
    return [None, None, None]


def normalize_companion_collection(value) -> dict:
    collection = parse_json_object(value)
    normalized = {}

    for companion_id, raw_entry in collection.items():
        if companion_id not in COMPANION_CATALOG:
            continue

        entry = raw_entry if isinstance(raw_entry, dict) else {}

        normalized[companion_id] = {
            "owned": bool(entry.get("owned", True)),
            "level": clamp_int(
                entry.get("level", 1),
                1,
                COMPANION_MAX_LEVEL,
            ),
            "fragments": max(
                0,
                int(entry.get("fragments", 0)),
            ),
        }

    return normalized


def normalize_companion_slots(
    value,
    collection: dict,
) -> list[str | None]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []

    if not isinstance(parsed, list):
        parsed = []

    slots = []
    used = set()

    for raw_companion_id in parsed[:3]:
        companion_id = str(raw_companion_id or "").strip()

        if (
            companion_id in COMPANION_CATALOG
            and companion_id in collection
            and bool(collection[companion_id].get("owned"))
            and companion_id not in used
        ):
            slots.append(companion_id)
            used.add(companion_id)
        else:
            slots.append(None)

    while len(slots) < 3:
        slots.append(None)

    return slots[:3]


def calculate_companion_effects(player: dict) -> dict:
    """Return combat bonuses from companions in unlocked active slots."""
    level = clamp_int(player.get("level", 1), 1, MAX_HERO_LEVEL)
    collection = normalize_companion_collection(
        player.get("companions_collection_json")
    )
    slots = normalize_companion_slots(
        player.get("companion_slots_json"), collection
    )
    unlocked_count = sum(
        level >= unlock_level
        for unlock_level in COMPANION_SLOT_UNLOCK_LEVELS
    )
    sources = active_companion_sources(slots, collection, unlocked_count)
    stats, custom = CombatEffectEngine.combine(sources)
    base_damage = max(1, int(player.get("damage", 1)))
    extra_attack_ratio = custom.get("extra_attack_ratio", 0.0)
    extra_attack_damage = (
        max(1, round(base_damage * extra_attack_ratio))
        if extra_attack_ratio > 0
        else 0
    )
    return {
        "damage_multiplier": round(stats.damage_multiplier, 4),
        "hp_multiplier": round(stats.max_hp_multiplier, 4),
        "extra_attack_damage": extra_attack_damage,
        "attack_speed_multiplier": round(stats.attack_speed_multiplier, 4),
        "skill_cooldown_multiplier": round(stats.cooldown_multiplier, 4),
        "crit_chance_bonus": round(stats.crit_chance_bonus, 4),
        "crit_damage_bonus": round(stats.crit_damage_bonus, 4),
        "victory_healing_ratio": round(custom.get("victory_healing_ratio", 0.0), 4),
        "active_effects": public_active_effects(
            sources,
            {key: value["name"] for key, value in COMPANION_CATALOG.items()},
        ),
    }


def companion_skill_cooldown(player: dict, base_seconds: float) -> float:
    return float(base_seconds) * float(
        calculate_companion_effects(player)["skill_cooldown_multiplier"]
    )


def default_skill_collection() -> dict:
    return {}


def default_skill_slots() -> list[str | None]:
    return [None, None, None]


def normalize_skill_collection(value) -> dict:
    collection = parse_json_object(value)
    normalized = {}

    for skill_id, raw_entry in collection.items():
        if not isinstance(skill_id, str):
            continue
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        normalized[skill_id] = {
            "owned": bool(entry.get("owned", True)),
            "level": clamp_int(entry.get("level", 1), 1, 20),
            "fragments": max(0, int(entry.get("fragments", 0))),
        }

    for skill_id, starter_entry in default_skill_collection().items():
        if skill_id not in normalized:
            normalized[skill_id] = starter_entry

    return normalized


def normalize_skill_slots(value, collection: dict) -> list[str | None]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []

    if not isinstance(parsed, list):
        parsed = []

    slots: list[str | None] = []
    used = set()

    for raw_skill_id in parsed[:3]:
        skill_id = str(raw_skill_id or "").strip()

        if (
            skill_id
            and skill_id in collection
            and bool(collection[skill_id].get("owned"))
            and skill_id not in used
        ):
            slots.append(skill_id)
            used.add(skill_id)
        else:
            slots.append(None)

    while len(slots) < 3:
        slots.append(None)

    return slots[:3]


def skill_summon_level_from_exp(summon_exp: int) -> int:
    summon_exp = max(0, int(summon_exp))
    level = 1

    for candidate_level in range(
        2,
        SKILL_SUMMON_MAX_LEVEL + 1,
    ):
        if summon_exp < SKILL_SUMMON_LEVEL_THRESHOLDS[
            candidate_level
        ]:
            break
        level = candidate_level

    return level


def skill_summon_progress(summon_exp: int) -> dict:
    summon_exp = max(0, int(summon_exp))
    level = skill_summon_level_from_exp(summon_exp)
    level_start = SKILL_SUMMON_LEVEL_THRESHOLDS[level]

    if level >= SKILL_SUMMON_MAX_LEVEL:
        return {
            "level": level,
            "exp": summon_exp,
            "current": 0,
            "required": 0,
            "remaining": 0,
            "progress": 1.0,
            "max_level": True,
        }

    next_threshold = SKILL_SUMMON_LEVEL_THRESHOLDS[level + 1]
    required = max(1, next_threshold - level_start)
    current = max(0, summon_exp - level_start)

    return {
        "level": level,
        "exp": summon_exp,
        "current": current,
        "required": required,
        "remaining": max(0, required - current),
        "progress": round(min(1.0, current / required), 4),
        "next_level_total_exp": next_threshold,
        "max_level": False,
    }


def public_summon_chances(summon_level: int) -> dict:
    summon_level = clamp_int(
        summon_level,
        1,
        SKILL_SUMMON_MAX_LEVEL,
    )
    weights = SKILL_SUMMON_RARITY_WEIGHTS[summon_level]

    return {
        rarity: {
            "name": SKILL_RARITY_NAMES[rarity],
            "chance": round(weight / 100, 2),
        }
        for rarity, weight in weights.items()
    }


def skill_fragments_required(skill_level: int) -> int:
    skill_level = clamp_int(skill_level, 1, SKILL_MAX_LEVEL)

    if skill_level >= SKILL_MAX_LEVEL:
        return 0

    return 5 * skill_level


def companion_summon_level_from_exp(
    summon_exp: int,
) -> int:
    summon_exp = max(0, int(summon_exp))
    level = 1

    for candidate_level in range(
        2,
        COMPANION_SUMMON_MAX_LEVEL + 1,
    ):
        if summon_exp < COMPANION_SUMMON_LEVEL_THRESHOLDS[
            candidate_level
        ]:
            break

        level = candidate_level

    return level


def companion_summon_progress(summon_exp: int) -> dict:
    summon_exp = max(0, int(summon_exp))
    level = companion_summon_level_from_exp(summon_exp)
    level_start = COMPANION_SUMMON_LEVEL_THRESHOLDS[level]

    if level >= COMPANION_SUMMON_MAX_LEVEL:
        return {
            "level": level,
            "exp": summon_exp,
            "current": 0,
            "required": 0,
            "remaining": 0,
            "progress": 1.0,
            "max_level": True,
        }

    next_threshold = COMPANION_SUMMON_LEVEL_THRESHOLDS[
        level + 1
    ]
    required = max(1, next_threshold - level_start)
    current = max(0, summon_exp - level_start)

    return {
        "level": level,
        "exp": summon_exp,
        "current": current,
        "required": required,
        "remaining": max(0, required - current),
        "progress": round(
            min(1.0, current / required),
            4,
        ),
        "next_level_total_exp": next_threshold,
        "max_level": False,
    }


def public_companion_summon_chances(
    summon_level: int,
) -> dict:
    summon_level = clamp_int(
        summon_level,
        1,
        COMPANION_SUMMON_MAX_LEVEL,
    )
    weights = COMPANION_SUMMON_RARITY_WEIGHTS[
        summon_level
    ]

    return {
        rarity: {
            "name": COMPANION_RARITY_NAMES[rarity],
            "chance": round(weight / 100, 2),
        }
        for rarity, weight in weights.items()
    }


def companion_fragments_required(
    companion_level: int,
) -> int:
    companion_level = clamp_int(
        companion_level,
        1,
        COMPANION_MAX_LEVEL,
    )

    if companion_level >= COMPANION_MAX_LEVEL:
        return 0

    return 5 * companion_level


def roll_companion_id(summon_level: int) -> str:
    summon_level = clamp_int(
        summon_level,
        1,
        COMPANION_SUMMON_MAX_LEVEL,
    )
    rarity_weights = COMPANION_SUMMON_RARITY_WEIGHTS[
        summon_level
    ]
    rarities = list(rarity_weights)

    rarity = random.choices(
        rarities,
        weights=[
            rarity_weights[rarity_name]
            for rarity_name in rarities
        ],
        k=1,
    )[0]

    candidates = [
        companion_id
        for companion_id, definition
        in COMPANION_CATALOG.items()
        if definition["rarity"] == rarity
    ]

    return random.choice(candidates)


def apply_companion_summon(
    collection: dict,
    companion_id: str,
) -> dict:
    definition = COMPANION_CATALOG[companion_id]
    rarity = str(definition["rarity"])
    entry = collection.get(companion_id)

    if (
        not isinstance(entry, dict)
        or not bool(entry.get("owned"))
    ):
        collection[companion_id] = {
            "owned": True,
            "level": 1,
            "fragments": 0,
        }

        return {
            "companion_id": companion_id,
            "name": definition["name"],
            "icon": definition["icon"],
            "rarity": rarity,
            "rarity_name": COMPANION_RARITY_NAMES[rarity],
            "new": True,
            "fragments_gained": 0,
            "levels_gained": 0,
            "level": 1,
            "fragments": 0,
        }

    companion_level = clamp_int(
        entry.get("level", 1),
        1,
        COMPANION_MAX_LEVEL,
    )
    fragments = max(
        0,
        int(entry.get("fragments", 0)),
    )
    fragments_gained = COMPANION_DUPLICATE_FRAGMENTS[
        rarity
    ]
    fragments += fragments_gained
    levels_gained = 0

    while companion_level < COMPANION_MAX_LEVEL:
        required = companion_fragments_required(
            companion_level
        )

        if required <= 0 or fragments < required:
            break

        fragments -= required
        companion_level += 1
        levels_gained += 1

    collection[companion_id] = {
        "owned": True,
        "level": companion_level,
        "fragments": fragments,
    }

    return {
        "companion_id": companion_id,
        "name": definition["name"],
        "icon": definition["icon"],
        "rarity": rarity,
        "rarity_name": COMPANION_RARITY_NAMES[rarity],
        "new": False,
        "fragments_gained": fragments_gained,
        "levels_gained": levels_gained,
        "level": companion_level,
        "fragments": fragments,
    }


# COMPANION_PUBLIC_API_V1
def public_companion_catalog() -> list[dict]:
    rarity_order = {
        "common": 0,
        "rare": 1,
        "epic": 2,
        "legendary": 3,
    }

    result = []

    for companion_id, definition in COMPANION_CATALOG.items():
        rarity = str(definition["rarity"])

        result.append(
            {
                "id": companion_id,
                "name": definition["name"],
                "icon": definition["icon"],
                "rarity": rarity,
                "rarity_name": COMPANION_RARITY_NAMES[rarity],
                "implemented": bool(
                    definition.get("implemented", False)
                ),
                "description": definition["description"],
            }
        )

    result.sort(
        key=lambda item: (
            rarity_order[item["rarity"]],
            item["name"],
        )
    )

    return result


def public_skill_catalog() -> list[dict]:
    rarity_order = {
        "common": 0,
        "rare": 1,
        "epic": 2,
        "legendary": 3,
    }
    result = []

    for skill_id, definition in SKILL_CATALOG.items():
        rarity = str(definition["rarity"])
        result.append(
            {
                "id": skill_id,
                "name": definition["name"],
                "icon": definition["icon"],
                "rarity": rarity,
                "rarity_name": SKILL_RARITY_NAMES[rarity],
                "implemented": bool(definition["implemented"]),
                "description": definition["description"],
            }
        )

    result.sort(
        key=lambda item: (
            rarity_order[item["rarity"]],
            item["name"],
        )
    )
    return result


def roll_skill_id(summon_level: int) -> str:
    summon_level = clamp_int(
        summon_level,
        1,
        SKILL_SUMMON_MAX_LEVEL,
    )
    rarity_weights = SKILL_SUMMON_RARITY_WEIGHTS[
        summon_level
    ]
    rarities = list(rarity_weights)

    rarity = random.choices(
        rarities,
        weights=[
            rarity_weights[rarity_name]
            for rarity_name in rarities
        ],
        k=1,
    )[0]

    candidates = [
        skill_id
        for skill_id, definition in SKILL_CATALOG.items()
        if definition["rarity"] == rarity
    ]
    return random.choice(candidates)


def apply_skill_summon(
    collection: dict,
    skill_id: str,
) -> dict:
    definition = SKILL_CATALOG[skill_id]
    rarity = str(definition["rarity"])
    entry = collection.get(skill_id)

    if not isinstance(entry, dict) or not bool(entry.get("owned")):
        collection[skill_id] = {
            "owned": True,
            "level": 1,
            "fragments": 0,
        }
        return {
            "skill_id": skill_id,
            "name": definition["name"],
            "icon": definition["icon"],
            "rarity": rarity,
            "rarity_name": SKILL_RARITY_NAMES[rarity],
            "new": True,
            "fragments_gained": 0,
            "levels_gained": 0,
            "level": 1,
            "fragments": 0,
        }

    skill_level = clamp_int(
        entry.get("level", 1),
        1,
        SKILL_MAX_LEVEL,
    )
    fragments = max(0, int(entry.get("fragments", 0)))
    fragments_gained = SKILL_DUPLICATE_FRAGMENTS[rarity]
    fragments += fragments_gained
    levels_gained = 0

    while skill_level < SKILL_MAX_LEVEL:
        required = skill_fragments_required(skill_level)

        if required <= 0 or fragments < required:
            break

        fragments -= required
        skill_level += 1
        levels_gained += 1

    collection[skill_id] = {
        "owned": True,
        "level": skill_level,
        "fragments": fragments,
    }

    return {
        "skill_id": skill_id,
        "name": definition["name"],
        "icon": definition["icon"],
        "rarity": rarity,
        "rarity_name": SKILL_RARITY_NAMES[rarity],
        "new": False,
        "fragments_gained": fragments_gained,
        "levels_gained": levels_gained,
        "level": skill_level,
        "fragments": fragments,
    }


def get_player_skill_state(
    player: dict,
    skill_id: str,
) -> dict:
    collection = normalize_skill_collection(
        player.get("skills_collection_json")
    )
    slots = normalize_skill_slots(
        player.get("skill_slots_json"),
        collection,
    )
    hero_level = clamp_int(
        player.get("level", 1),
        1,
        MAX_HERO_LEVEL,
    )
    unlocked_slot_count = sum(
        1
        for unlock_level in SKILL_SLOT_UNLOCK_LEVELS
        if hero_level >= unlock_level
    )

    entry = collection.get(skill_id, {})
    owned = bool(entry.get("owned", False))
    skill_level = clamp_int(entry.get("level", 1), 1, 20)

    slot_index = next(
        (
            index
            for index, equipped_skill_id in enumerate(slots)
            if equipped_skill_id == skill_id
        ),
        None,
    )
    equipped = slot_index is not None
    slot_unlocked = (
        slot_index is not None
        and slot_index < unlocked_slot_count
    )

    return {
        "owned": owned,
        "level": skill_level,
        "equipped": equipped,
        "slot_index": (
            slot_index + 1
            if slot_index is not None
            else None
        ),
        "slot_unlocked": slot_unlocked,
        "available": owned and equipped and slot_unlocked,
    }


def skill_power_multiplier(skill_level: int) -> float:
    skill_level = clamp_int(skill_level, 1, 20)
    return 1.0 + (
        (skill_level - 1)
        * SKILL_POWER_PER_LEVEL
    )


def unavailable_skill_message(
    state: dict,
    skill_name: str,
) -> str:
    if not state["owned"]:
        return f"🔒 Навык {skill_name} ещё не получен"
    if not state["equipped"]:
        return f"🔒 Установите {skill_name} в боевой слот"
    if not state["slot_unlocked"]:
        slot_index = int(state.get("slot_index") or 1)
        unlock_level = SKILL_SLOT_UNLOCK_LEVELS[slot_index - 1]
        return (
            f"🔒 Слот {slot_index} откроется "
            f"на {unlock_level} уровне"
        )
    return f"🔒 Навык {skill_name} недоступен"


def ensure_player_companion_data(
    connection: sqlite3.Connection,
    telegram_id: int,
) -> None:
    row = connection.execute(
        """
        SELECT level,
               companions_collection_json,
               companion_slots_json
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if row is None:
        return

    collection = normalize_companion_collection(
        row["companions_collection_json"]
    )
    slots = normalize_companion_slots(
        row["companion_slots_json"],
        collection,
    )

    level = max(1, int(row["level"] or 1))
    unlocked_count = sum(
        level >= unlock_level
        for unlock_level in COMPANION_SLOT_UNLOCK_LEVELS
    )

    for index in range(unlocked_count, 3):
        slots[index] = None

    connection.execute(
        """
        UPDATE players
        SET companions_collection_json = ?,
            companion_slots_json = ?
        WHERE telegram_id = ?
        """,
        (
            json.dumps(
                collection,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            json.dumps(
                slots,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            telegram_id,
        ),
    )


def ensure_player_skill_data(
    connection: sqlite3.Connection,
    telegram_id: int,
) -> None:
    row = connection.execute(
        """
        SELECT level, skills_collection_json, skill_slots_json
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if row is None:
        return

    collection = normalize_skill_collection(
        row["skills_collection_json"]
    )
    slots = normalize_skill_slots(
        row["skill_slots_json"],
        collection,
    )

    level = max(1, int(row["level"] or 1))
    unlocked_count = sum(
        level >= unlock_level
        for unlock_level in SKILL_SLOT_UNLOCK_LEVELS
    )

    for index in range(unlocked_count, 3):
        slots[index] = None

    connection.execute(
        """
        UPDATE players
        SET skills_collection_json = ?,
            skill_slots_json = ?
        WHERE telegram_id = ?
        """,
        (
            json.dumps(
                collection,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            json.dumps(
                slots,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            telegram_id,
        ),
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
        ensure_player_skill_data(connection, telegram_id)
        ensure_player_companion_data(connection, telegram_id)
        ensure_daily_quest_state(
            connection,
            telegram_id,
            now,
        )
        ensure_chest_boss_state(
            connection,
            telegram_id,
            now,
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


def current_daily_quest_date(now: int | float | None = None) -> str:
    timestamp = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def normalize_daily_claims(value: object) -> dict:
    parsed = parse_json_object(value)
    return {
        quest_id: bool(parsed.get(quest_id, False))
        for quest_id in DAILY_QUEST_DEFINITIONS
    }


def ensure_daily_quest_state(
    connection: sqlite3.Connection,
    telegram_id: int,
    now: int | float | None = None,
) -> None:
    today = current_daily_quest_date(now)
    row = connection.execute(
        """
        SELECT daily_quest_date
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if row is None:
        return

    stored_date = str(row["daily_quest_date"] or "")

    if stored_date == today:
        return

    connection.execute(
        """
        UPDATE players
        SET daily_quest_date = ?,
            daily_kills = 0,
            daily_bosses = 0,
            daily_chests_opened = 0,
            daily_quests_claimed_json = '{}',
            daily_all_claimed = 0
        WHERE telegram_id = ?
        """,
        (today, telegram_id),
    )


def build_daily_quests(player: dict) -> dict:
    claims = normalize_daily_claims(
        player.get("daily_quests_claimed_json")
    )
    companion_rewards_unlocked = (
        int(player.get("level", 1))
        >= COMPANION_SYSTEM_UNLOCK_LEVEL
    )
    quests = []
    completed_count = 0
    claimed_count = 0

    for quest_id, definition in DAILY_QUEST_DEFINITIONS.items():
        progress = max(
            0,
            int(player.get(definition["counter"], 0)),
        )
        target = int(definition["target"])
        completed = progress >= target
        claimed = bool(claims.get(quest_id, False))

        if completed:
            completed_count += 1
        if claimed:
            claimed_count += 1

        quests.append(
            {
                "id": quest_id,
                "name": definition["name"],
                "description": definition["description"],
                "icon": definition["icon"],
                "progress": min(progress, target),
                "raw_progress": progress,
                "target": target,
                "completed": completed,
                "claimed": claimed,
                "claimable": completed and not claimed,
                "reward": {
                    "skill_scrolls": int(
                        definition["scrolls"]
                    ),
                    "companion_scrolls": (
                        int(definition["scrolls"])
                        if companion_rewards_unlocked
                        else 0
                    ),
                },
            }
        )

    all_claimed = bool(
        int(player.get("daily_all_claimed", 0))
    )
    bonus_unlocked = claimed_count == len(
        DAILY_QUEST_DEFINITIONS
    )

    return {
        "type": "daily",
        "date": str(
            player.get("daily_quest_date")
            or current_daily_quest_date()
        ),
        "reset_timezone": "UTC",
        "quests": quests,
        "completed_count": completed_count,
        "claimed_count": claimed_count,
        "total_count": len(DAILY_QUEST_DEFINITIONS),
        "bonus": {
            "id": DAILY_ALL_QUESTS_ID,
            "name": "Все ежедневные задания",
            "description": "Заберите награды всех ежедневных заданий",
            "icon": "🎁",
            "completed": bonus_unlocked,
            "claimed": all_claimed,
            "claimable": bonus_unlocked and not all_claimed,
            "reward": {
                "skill_scrolls": DAILY_ALL_QUESTS_REWARD_SCROLLS,
                "companion_scrolls": (
                    DAILY_ALL_QUESTS_REWARD_COMPANION_SCROLLS
                    if companion_rewards_unlocked
                    else 0
                ),
            },
        },
    }



# CHEST_CHALLENGE_BOSS_V1

CHEST_BOSS_FREE_ATTEMPTS = 3
CHEST_BOSS_AD_ATTEMPTS = 2
CHEST_BOSS_MAX_LEVEL = 500


def current_chest_boss_date(
    now: int | float | None = None,
) -> str:
    timestamp = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def calculate_chest_boss_hp(level: int) -> int:
    level = clamp_int(level, 1, CHEST_BOSS_MAX_LEVEL)
    value = round(80 * (1.18 ** (level - 1)))
    return clamp_int(value, 1, MAX_SAFE_STAT)


def calculate_chest_boss_damage(level: int) -> int:
    level = clamp_int(level, 1, CHEST_BOSS_MAX_LEVEL)
    value = round(5 * (1.12 ** (level - 1)))
    return clamp_int(value, 1, MAX_SAFE_STAT)


def calculate_chest_boss_reward(level: int) -> int:
    level = clamp_int(level, 1, CHEST_BOSS_MAX_LEVEL)

    # Уровень 1 = 2 сундука.
    # Каждый следующий уровень даёт на 1 сундук больше.
    return level + 1


def ensure_chest_boss_state(
    connection: sqlite3.Connection,
    telegram_id: int,
    now: int | float | None = None,
) -> None:
    today = current_chest_boss_date(now)

    row = connection.execute(
        """
        SELECT chest_boss_attempt_date
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if row is None:
        return

    stored_date = str(row["chest_boss_attempt_date"] or "")

    if stored_date == today:
        return

    connection.execute(
        """
        UPDATE players
        SET chest_boss_attempt_date = ?,
            chest_boss_free_attempts_used = 0,
            chest_boss_ad_attempts_used = 0,
            chest_boss_active = 0,
            chest_boss_hp = 0,
            chest_boss_max_hp = 0,
            chest_boss_hero_hp = 0
        WHERE telegram_id = ?
        """,
        (today, telegram_id),
    )


def build_chest_boss_state(player: dict) -> dict:
    level = clamp_int(
        player.get("chest_boss_level", 1),
        1,
        CHEST_BOSS_MAX_LEVEL,
    )

    free_used = max(
        0,
        int(player.get("chest_boss_free_attempts_used", 0)),
    )
    ad_used = max(
        0,
        int(player.get("chest_boss_ad_attempts_used", 0)),
    )
    bonus_attempts = max(
        0,
        int(player.get("chest_boss_bonus_attempts", 0)),
    )

    free_remaining = max(
        0,
        CHEST_BOSS_FREE_ATTEMPTS - free_used,
    )
    ads_remaining = max(
        0,
        CHEST_BOSS_AD_ATTEMPTS - ad_used,
    )

    active = bool(int(player.get("chest_boss_active", 0)))

    return {
        "type": "chest_boss",
        "name": "Хранитель сундуков",
        "level": level,
        "max_level": CHEST_BOSS_MAX_LEVEL,
        "active": active,
        "boss_hp": max(
            0,
            int(player.get("chest_boss_hp", 0)),
        ),
        "boss_max_hp": max(
            0,
            int(player.get("chest_boss_max_hp", 0)),
        ),
        "boss_damage": calculate_chest_boss_damage(level),
        "hero_hp": max(
            0,
            int(player.get("chest_boss_hero_hp", 0)),
        ),
        "hero_max_hp": max(
            1,
            int(player.get("hero_max_hp", 1)),
        ),
        "reward": {
            "chests": calculate_chest_boss_reward(level),
        },
        "attempts": {
            "free_total": CHEST_BOSS_FREE_ATTEMPTS,
            "free_used": free_used,
            "free_remaining": free_remaining,
            "ad_total": CHEST_BOSS_AD_ATTEMPTS,
            "ad_used": ad_used,
            "ad_remaining": ads_remaining,
            "bonus": bonus_attempts,
            "available": free_remaining + bonus_attempts,
        },
        "keys": max(
            0,
            int(player.get("chest_boss_keys", 0)),
        ),
        "attempt_date": str(
            player.get("chest_boss_attempt_date")
            or current_chest_boss_date()
        ),
        "reset_timezone": "UTC",
    }


def consume_chest_boss_attempt(
    connection: sqlite3.Connection,
    player: dict,
) -> str | None:
    telegram_id = int(player["telegram_id"])

    free_used = max(
        0,
        int(player.get("chest_boss_free_attempts_used", 0)),
    )

    bonus_attempts = max(
        0,
        int(player.get("chest_boss_bonus_attempts", 0)),
    )

    if free_used < CHEST_BOSS_FREE_ATTEMPTS:
        connection.execute(
            """
            UPDATE players
            SET chest_boss_free_attempts_used =
                    chest_boss_free_attempts_used + 1
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        return "free"

    if bonus_attempts > 0:
        connection.execute(
            """
            UPDATE players
            SET chest_boss_bonus_attempts =
                    chest_boss_bonus_attempts - 1
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        return "bonus"

    return None




# GEM_CHALLENGE_BOSS_V1

GEM_BOSS_FREE_ATTEMPTS = 2
GEM_BOSS_AD_ATTEMPTS = 2
GEM_BOSS_MAX_LEVEL = 500

# GEM_BOSS_FULL_ATTEMPTS_V1


def current_gem_boss_date(
    now: int | float | None = None,
) -> str:
    timestamp = time.time() if now is None else float(now)
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp))


def calculate_gem_boss_hp(level: int) -> int:
    level = clamp_int(level, 1, GEM_BOSS_MAX_LEVEL)
    value = round(110 * (1.19 ** (level - 1)))
    return clamp_int(value, 1, MAX_SAFE_STAT)


def calculate_gem_boss_damage(level: int) -> int:
    level = clamp_int(level, 1, GEM_BOSS_MAX_LEVEL)
    value = round(6 * (1.13 ** (level - 1)))
    return clamp_int(value, 1, MAX_SAFE_STAT)


def calculate_gem_boss_reward(level: int) -> int:
    level = clamp_int(level, 1, GEM_BOSS_MAX_LEVEL)

    # Временные значения до общего этапа балансировки.
    return 5 + level


def ensure_gem_boss_state(
    connection: sqlite3.Connection,
    telegram_id: int,
    now: int | float | None = None,
) -> None:
    today = current_gem_boss_date(now)

    row = connection.execute(
        """
        SELECT gem_boss_attempt_date
        FROM players
        WHERE telegram_id = ?
        """,
        (telegram_id,),
    ).fetchone()

    if row is None:
        return

    stored_date = str(row["gem_boss_attempt_date"] or "")

    if stored_date == today:
        return

    connection.execute(
        """
        UPDATE players
        SET gem_boss_attempt_date = ?,
            gem_boss_free_attempts_used = 0,
            gem_boss_ad_attempts_used = 0,
            gem_boss_active = 0,
            gem_boss_hp = 0,
            gem_boss_max_hp = 0,
            gem_boss_hero_hp = 0
        WHERE telegram_id = ?
        """,
        (
            today,
            telegram_id,
        ),
    )


def build_gem_boss_state(player: dict) -> dict:
    level = clamp_int(
        player.get("gem_boss_level", 1),
        1,
        GEM_BOSS_MAX_LEVEL,
    )

    free_used = max(
        0,
        int(player.get("gem_boss_free_attempts_used", 0)),
    )

    free_remaining = max(
        0,
        GEM_BOSS_FREE_ATTEMPTS - free_used,
    )

    ad_used = max(
        0,
        int(player.get("gem_boss_ad_attempts_used", 0)),
    )

    ad_remaining = max(
        0,
        GEM_BOSS_AD_ATTEMPTS - ad_used,
    )

    bonus_attempts = max(
        0,
        int(player.get("gem_boss_bonus_attempts", 0)),
    )

    active = bool(
        int(player.get("gem_boss_active", 0))
    )

    return {
        "type": "gem_boss",
        "name": "Страж самоцветов",
        "level": level,
        "max_level": GEM_BOSS_MAX_LEVEL,
        "active": active,
        "boss_hp": max(
            0,
            int(player.get("gem_boss_hp", 0)),
        ),
        "boss_max_hp": max(
            0,
            int(player.get("gem_boss_max_hp", 0)),
        ),
        "boss_damage": calculate_gem_boss_damage(level),
        "hero_hp": max(
            0,
            int(player.get("gem_boss_hero_hp", 0)),
        ),
        "hero_max_hp": max(
            1,
            int(player.get("hero_max_hp", 1)),
        ),
        "reward": {
            "gems": calculate_gem_boss_reward(level),
        },
        "attempts": {
            "free_total": GEM_BOSS_FREE_ATTEMPTS,
            "free_used": free_used,
            "free_remaining": free_remaining,
            "ad_total": GEM_BOSS_AD_ATTEMPTS,
            "ad_used": ad_used,
            "ad_remaining": ad_remaining,
            "bonus": bonus_attempts,
            "available": free_remaining + bonus_attempts,
        },
        "keys": max(
            0,
            int(player.get("gem_boss_keys", 0)),
        ),
        "attempt_date": str(
            player.get("gem_boss_attempt_date")
            or current_gem_boss_date()
        ),
        "reset_timezone": "UTC",
    }


def consume_gem_boss_attempt(
    connection: sqlite3.Connection,
    player: dict,
) -> bool:
    telegram_id = int(player["telegram_id"])

    free_used = max(
        0,
        int(player.get("gem_boss_free_attempts_used", 0)),
    )

    if free_used < GEM_BOSS_FREE_ATTEMPTS:
        connection.execute(
            """
            UPDATE players
            SET gem_boss_free_attempts_used =
                    gem_boss_free_attempts_used + 1
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        return True

    bonus_attempts = max(
        0,
        int(player.get("gem_boss_bonus_attempts", 0)),
    )

    if bonus_attempts > 0:
        connection.execute(
            """
            UPDATE players
            SET gem_boss_bonus_attempts =
                    gem_boss_bonus_attempts - 1
            WHERE telegram_id = ?
            """,
            (telegram_id,),
        )
        return True

    return False


def public_equipment(player: dict) -> dict:
    stored = parse_json_object(player.get("equipment_json"))
    return {slot_key: stored.get(slot_key) for slot_key in GEAR_SLOTS}


def public_pending_loot(player: dict) -> dict | None:
    pending = parse_json_object(player.get("pending_loot_json"))
    return pending or None


def build_player_response(player: dict, **extra) -> dict:
    current_time = time.time()
    attack_speed = max(0.1, float(player.get("attack_speed", 1.0)))
    companion_effects = calculate_companion_effects(player)
    attack_interval = max(
        MIN_ATTACK_INTERVAL,
        1 / (attack_speed * float(companion_effects["attack_speed_multiplier"])),
    )
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

    skill_collection = normalize_skill_collection(
        player.get("skills_collection_json")
    )
    equipped_skill_slots = normalize_skill_slots(
        player.get("skill_slots_json"),
        skill_collection,
    )
    unlocked_skill_slot_count = sum(
        1
        for unlock_level in SKILL_SLOT_UNLOCK_LEVELS
        if level >= unlock_level
    )

    summon_exp = max(
        0,
        int(player.get("skill_summon_exp", 0)),
    )
    summon_progress = skill_summon_progress(summon_exp)
    summon_level = int(summon_progress["level"])

    spore_skill_state = get_player_skill_state(
        player,
        "spore_strike",
    )
    shield_skill_state = get_player_skill_state(
        player,
        "mushroom_shield",
    )
    poison_skill_state = get_player_skill_state(
        player,
        "poison_cloud",
    )

    spore_damage_multiplier = (
        SPORE_STRIKE_DAMAGE_MULTIPLIER
        * skill_power_multiplier(spore_skill_state["level"])
    )
    shield_hp_ratio = (
        MUSHROOM_SHIELD_HP_RATIO
        * skill_power_multiplier(shield_skill_state["level"])
    )
    poison_damage_multiplier = (
        POISON_CLOUD_DAMAGE_MULTIPLIER
        * skill_power_multiplier(poison_skill_state["level"])
    )

    spore_last_used_at = float(
        player.get("spore_strike_last_used_at", 0)
    )
    spore_cooldown_seconds = companion_skill_cooldown(
        player, SPORE_STRIKE_COOLDOWN_SECONDS
    )
    spore_elapsed = time.time() - spore_last_used_at
    spore_cooldown_remaining = (
        0.0
        if spore_last_used_at <= 0
        else max(
            0.0,
            spore_cooldown_seconds - spore_elapsed,
        )
    )
    shield_last_used_at = float(
        player.get("mushroom_shield_last_used_at", 0)
    )
    shield_cooldown_seconds = companion_skill_cooldown(
        player, MUSHROOM_SHIELD_COOLDOWN_SECONDS
    )
    shield_elapsed = time.time() - shield_last_used_at
    shield_cooldown_remaining = (
        0.0
        if shield_last_used_at <= 0
        else max(
            0.0,
            shield_cooldown_seconds - shield_elapsed,
        )
    )
    companion_effects_for_hp = calculate_companion_effects(player)
    effective_max_hp_for_skills = max(
        1,
        round(
            int(player.get("hero_max_hp", 1))
            * companion_effects_for_hp["hp_multiplier"]
        ),
    )
    shield_capacity = max(
        1,
        round(
            effective_max_hp_for_skills
            * shield_hp_ratio
            * (
                BATTLE_STATES.peek(
                    int(player.get("telegram_id", 0)), battle_identity(player), current_time
                ).shield_capacity_multiplier
                if BATTLE_STATES.peek(
                    int(player.get("telegram_id", 0)), battle_identity(player), current_time
                ) is not None
                else 1.0
            )
        ),
    )
    shield_amount = max(
        0,
        min(
            shield_capacity,
            int(player.get("mushroom_shield_amount", 0)),
        ),
    )

    poison_last_used_at = float(
        player.get("poison_cloud_last_used_at", 0)
    )
    poison_cooldown_seconds = companion_skill_cooldown(
        player, POISON_CLOUD_COOLDOWN_SECONDS
    )
    poison_until = float(player.get("poison_cloud_until", 0))
    poison_next_tick_at = float(
        player.get("poison_cloud_next_tick_at", 0)
    )
    poison_cooldown_remaining = (
        0.0
        if poison_last_used_at <= 0
        else max(
            0.0,
            poison_cooldown_seconds
            - (current_time - poison_last_used_at),
        )
    )
    poison_duration_remaining = max(
        0.0,
        poison_until - current_time,
    )
    poison_active = (
        poison_duration_remaining > 0
        and poison_next_tick_at > 0
    )

    companion_collection = normalize_companion_collection(
        player.get("companions_collection_json")
    )
    companion_summon_exp = max(
        0,
        int(player.get("companion_summon_exp", 0)),
    )
    companion_summon_level = (
        companion_summon_level_from_exp(
            companion_summon_exp
        )
    )
    companion_summon_state = companion_summon_progress(
        companion_summon_exp
    )
    companion_slots = normalize_companion_slots(
        player.get("companion_slots_json"),
        companion_collection,
    )
    unlocked_companion_slot_count = sum(
        level >= unlock_level
        for unlock_level in COMPANION_SLOT_UNLOCK_LEVELS
    )
    battle_effects = public_battle_effects(player)
    hp_multiplier = float(companion_effects["hp_multiplier"])
    effective_hero_max_hp = max(
        1,
        round(int(player.get("hero_max_hp", 1)) * hp_multiplier),
    )
    effective_hero_hp = max(
        0,
        min(
            effective_hero_max_hp,
            round(int(player.get("hero_hp", 0)) * hp_multiplier),
        ),
    )

    hidden_fields = {
        "equipment_json",
        "pending_loot_json",
        "skills_collection_json",
        "skill_slots_json",
        "companions_collection_json",
        "companion_slots_json",
        "daily_quests_claimed_json",
    }
    public_player = {
        key: value
        for key, value in player.items()
        if key not in hidden_fields
    }
    public_player["hero_hp"] = effective_hero_hp
    public_player["hero_max_hp"] = effective_hero_max_hp
    stats["total"]["hero_max_hp"] = effective_hero_max_hp
    stats["total"]["crit_chance"] = min(
        100.0,
        float(stats["total"]["crit_chance"])
        + float(companion_effects["crit_chance_bonus"]),
    )
    response = {
        **public_player,
        "equipment": equipment,
        "quests": build_daily_quests(player),
        "chest_boss": build_chest_boss_state(player),
        "gem_boss": build_gem_boss_state(player),
        "pending_loot": pending_loot,
        "pending_loot_comparison": (
            compare_loot(player, pending_loot) if pending_loot else None
        ),
        "skill_system": {
            "unlocked": level >= SKILL_SYSTEM_UNLOCK_LEVEL,
            "unlock_level": SKILL_SYSTEM_UNLOCK_LEVEL,
            "scrolls": max(
                0,
                int(player.get("skill_scrolls", 0)),
            ),
            "slot_unlock_levels": list(SKILL_SLOT_UNLOCK_LEVELS),
            "unlocked_slot_count": unlocked_skill_slot_count,
            "summon": {
                "single_cost": SKILL_SUMMON_COST,
                "ten_cost": SKILL_SUMMON_COST * 10,
                "allowed_counts": [1, 10],
                "level": summon_level,
                "max_level": SKILL_SUMMON_MAX_LEVEL,
                "exp": summon_exp,
                "progress": summon_progress,
                "rarity_chances": public_summon_chances(
                    summon_level
                ),
            },
            "catalog": public_skill_catalog(),
            "slots": [
                {
                    "index": index + 1,
                    "unlocked": index < unlocked_skill_slot_count,
                    "unlock_level": SKILL_SLOT_UNLOCK_LEVELS[index],
                    "skill_id": skill_id,
                }
                for index, skill_id in enumerate(equipped_skill_slots)
            ],
            "collection": {
                skill_id: {
                    **entry,
                    "fragments_required": (
                        skill_fragments_required(
                            int(entry.get("level", 1))
                        )
                    ),
                    "max_level": (
                        int(entry.get("level", 1))
                        >= SKILL_MAX_LEVEL
                    ),
                }
                for skill_id, entry in skill_collection.items()
            },
        },
        "companion_system": {
            "unlocked": (
                level >= COMPANION_SYSTEM_UNLOCK_LEVEL
            ),
            "unlock_level": COMPANION_SYSTEM_UNLOCK_LEVEL,
            "scrolls": max(
                0,
                int(player.get("companion_scrolls", 0)),
            ),
            "slot_unlock_levels": list(
                COMPANION_SLOT_UNLOCK_LEVELS
            ),
            "unlocked_slot_count": (
                unlocked_companion_slot_count
            ),
            "summon": {
                "single_cost": COMPANION_SUMMON_COST,
                "ten_cost": COMPANION_SUMMON_COST * 10,
                "allowed_counts": [1, 10],
                "level": companion_summon_level,
                "max_level": COMPANION_SUMMON_MAX_LEVEL,
                "exp": companion_summon_exp,
                "progress": companion_summon_state,
                "rarity_chances": (
                    public_companion_summon_chances(
                        companion_summon_level
                    )
                ),
            },
            "catalog": public_companion_catalog(),
            "slots": [
                {
                    "index": index + 1,
                    "unlocked": (
                        index
                        < unlocked_companion_slot_count
                    ),
                    "unlock_level": (
                        COMPANION_SLOT_UNLOCK_LEVELS[index]
                    ),
                    "companion_id": companion_id,
                }
                for index, companion_id
                in enumerate(companion_slots)
            ],
            "collection": companion_collection,
        },
        "companion_effects": companion_effects,
        "battle_effects": battle_effects,
        "stage_sequence": stage_sequence_state(player),
        "hero_stats": stats,
        "crit_chance": stats["total"]["crit_chance"],
        "crit_damage": stats["total"]["crit_damage"],
        "attack_interval": attack_interval,
        "skills": {
            "spore_strike": {
                **spore_skill_state,
                "unlocked": spore_skill_state["available"],
                "unlock_level": (
                    SKILL_SLOT_UNLOCK_LEVELS[
                        max(
                            0,
                            int(spore_skill_state.get("slot_index") or 1) - 1,
                        )
                    ]
                ),
                "damage_multiplier": round(
                    spore_damage_multiplier,
                    3,
                ),
                "cooldown_seconds": spore_cooldown_seconds,
                "cooldown_remaining": spore_cooldown_remaining,
                "ready": (
                    spore_skill_state["available"]
                    and spore_cooldown_remaining <= 0
                ),
            },
            "mushroom_shield": {
                **shield_skill_state,
                "unlocked": shield_skill_state["available"],
                "unlock_level": (
                    SKILL_SLOT_UNLOCK_LEVELS[
                        max(
                            0,
                            int(shield_skill_state.get("slot_index") or 1) - 1,
                        )
                    ]
                ),
                "hp_ratio": round(shield_hp_ratio, 4),
                "cooldown_seconds": shield_cooldown_seconds,
                "cooldown_remaining": shield_cooldown_remaining,
                "capacity": shield_capacity,
                "amount": shield_amount,
                "active": shield_amount > 0,
                "ready": (
                    shield_skill_state["available"]
                    and shield_cooldown_remaining <= 0
                ),
            },
            "poison_cloud": {
                **poison_skill_state,
                "unlocked": poison_skill_state["available"],
                "unlock_level": (
                    SKILL_SLOT_UNLOCK_LEVELS[
                        max(
                            0,
                            int(poison_skill_state.get("slot_index") or 1) - 1,
                        )
                    ]
                ),
                "damage_multiplier": round(
                    poison_damage_multiplier,
                    3,
                ),
                "duration_seconds": POISON_CLOUD_DURATION_SECONDS,
                "tick_seconds": POISON_CLOUD_TICK_SECONDS,
                "cooldown_seconds": poison_cooldown_seconds,
                "cooldown_remaining": poison_cooldown_remaining,
                "duration_remaining": poison_duration_remaining,
                "active": poison_active,
                "ready": (
                    poison_skill_state["available"]
                    and poison_cooldown_remaining <= 0
                    and not poison_active
                ),
            },
        },
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
        ("skills_auto_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("spore_strike_last_used_at", "REAL NOT NULL DEFAULT 0"),
        ("mushroom_shield_amount", "INTEGER NOT NULL DEFAULT 0"),
        ("mushroom_shield_last_used_at", "REAL NOT NULL DEFAULT 0"),
        ("poison_cloud_last_used_at", "REAL NOT NULL DEFAULT 0"),
        ("poison_cloud_until", "REAL NOT NULL DEFAULT 0"),
        ("poison_cloud_next_tick_at", "REAL NOT NULL DEFAULT 0"),
        (
            "skill_scrolls",
            f"INTEGER NOT NULL DEFAULT {STARTER_SKILL_SCROLLS}",
        ),
        (
            "skills_collection_json",
            "TEXT NOT NULL DEFAULT '{}'",
        ),
        (
            "skill_slots_json",
            "TEXT NOT NULL DEFAULT '[]'",
        ),
        (
            "skill_summon_level",
            "INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "skill_summon_exp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "companion_scrolls",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "companions_collection_json",
            "TEXT NOT NULL DEFAULT '{}'",
        ),
        (
            "companion_slots_json",
            "TEXT NOT NULL DEFAULT '[]'",
        ),
        (
            "companion_summon_exp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "daily_quest_date",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "daily_kills",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "daily_bosses",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "daily_chests_opened",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "daily_quests_claimed_json",
            "TEXT NOT NULL DEFAULT '{}'",
        ),
        (
            "daily_all_claimed",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_level",
            "INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "chest_boss_attempt_date",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "chest_boss_free_attempts_used",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_ad_attempts_used",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_bonus_attempts",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_keys",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_active",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_hp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_max_hp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "chest_boss_hero_hp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gems",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_level",
            "INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "gem_boss_attempt_date",
            "TEXT NOT NULL DEFAULT ''",
        ),
        (
            "gem_boss_free_attempts_used",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_ad_attempts_used",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_bonus_attempts",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_keys",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_active",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_hp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_max_hp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "gem_boss_hero_hp",
            "INTEGER NOT NULL DEFAULT 0",
        ),
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


@app.post("/quests/claim")
def claim_daily_quest(
    quest_id: str,
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    quest_id = str(quest_id or "").strip()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        ensure_daily_quest_state(
            connection,
            telegram_id,
            time.time(),
        )
        current = load_player(connection, telegram_id)
        claims = normalize_daily_claims(
            current.get("daily_quests_claimed_json")
        )

        if quest_id == DAILY_ALL_QUESTS_ID:
            if bool(int(current.get("daily_all_claimed", 0))):
                connection.commit()
                return build_player_response(
                    current,
                    quest_claimed=False,
                    message="🎁 Общая награда уже получена",
                )

            if not all(
                bool(claims.get(item_id, False))
                for item_id in DAILY_QUEST_DEFINITIONS
            ):
                connection.commit()
                return build_player_response(
                    current,
                    quest_claimed=False,
                    message=(
                        "Сначала заберите награды "
                        "всех ежедневных заданий"
                    ),
                )

            reward_scrolls = DAILY_ALL_QUESTS_REWARD_SCROLLS
            reward_companion_scrolls = (
                DAILY_ALL_QUESTS_REWARD_COMPANION_SCROLLS
                if int(current.get("level", 1))
                >= COMPANION_SYSTEM_UNLOCK_LEVEL
                else 0
            )

            connection.execute(
                """
                UPDATE players
                SET skill_scrolls = skill_scrolls + ?,
                    companion_scrolls = companion_scrolls + ?,
                    daily_all_claimed = 1,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    reward_scrolls,
                    reward_companion_scrolls,
                    int(time.time()),
                    telegram_id,
                ),
            )
            quest_name = "Все ежедневные задания"

        else:
            definition = DAILY_QUEST_DEFINITIONS.get(quest_id)

            if definition is None:
                raise HTTPException(
                    status_code=404,
                    detail="Задание не найдено",
                )

            if bool(claims.get(quest_id, False)):
                connection.commit()
                return build_player_response(
                    current,
                    quest_claimed=False,
                    message="Награда этого задания уже получена",
                )

            progress = max(
                0,
                int(current.get(definition["counter"], 0)),
            )
            target = int(definition["target"])

            if progress < target:
                connection.commit()
                return build_player_response(
                    current,
                    quest_claimed=False,
                    quest_id=quest_id,
                    quest_progress=progress,
                    quest_target=target,
                    message=(
                        f"Задание ещё не выполнено: "
                        f"{progress}/{target}"
                    ),
                )

            claims[quest_id] = True
            reward_scrolls = int(definition["scrolls"])
            reward_companion_scrolls = (
                reward_scrolls
                if int(current.get("level", 1))
                >= COMPANION_SYSTEM_UNLOCK_LEVEL
                else 0
            )
            quest_name = str(definition["name"])

            connection.execute(
                """
                UPDATE players
                SET skill_scrolls = skill_scrolls + ?,
                    companion_scrolls = companion_scrolls + ?,
                    daily_quests_claimed_json = ?,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    reward_scrolls,
                    reward_companion_scrolls,
                    json.dumps(
                        claims,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
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
        quest_claimed=True,
        quest_id=quest_id,
        quest_reward_scrolls=reward_scrolls,
        quest_reward_companion_scrolls=(
            reward_companion_scrolls
        ),
        message=(
            f"✅ {quest_name}: "
            f"+{reward_scrolls} свитк. навыков"
            + (
                f", +{reward_companion_scrolls} "
                f"свитк. спутников"
                if reward_companion_scrolls > 0
                else ""
            )
        ),
    )


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
        total_exp = max(0, int(current["experience"]))
        new_level = max(
            int(current.get("level", 1)),
            level_from_total_exp(total_exp),
        )
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
        claimed_experience=0,
        message=f"🎁 Получено: {pending_chests} сундуков",
    )


@app.post("/attack")
def attack(
    x_telegram_init_data: str = Header(...),
    skill: str | None = None,
    battle_id: str | None = None,
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        if battle_id is not None and battle_id != public_battle_identity(current):
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                stale_battle=True,
                message="Этот бой уже завершён",
            )
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
        rate_effects = calculate_companion_effects(current)
        attack_interval = max(
            MIN_ATTACK_INTERVAL,
            1 / (attack_speed * float(rate_effects["attack_speed_multiplier"])),
        )
        elapsed = now - float(current["last_attack_at"])
        if current["last_attack_at"] > 0 and elapsed < attack_interval:
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                retry_after=attack_interval - elapsed,
                message="Атака ещё не готова",
            )
        requested_skill = str(skill or "").strip().lower()
        attack_skill_ids = {
            effect_id
            for effect_id, definition in SKILL_EFFECTS.items()
            if definition["event"].value == "before_skill"
        }
        if requested_skill and requested_skill not in attack_skill_ids:
            raise HTTPException(status_code=400, detail="Неизвестный навык")

        level = int(current.get("level", 1))
        spore_state = get_player_skill_state(
            current,
            "spore_strike",
        )
        spore_unlocked = spore_state["available"]
        spore_last_used_at = float(
            current.get("spore_strike_last_used_at", 0)
        )
        spore_elapsed = now - spore_last_used_at
        companion_effects = calculate_companion_effects(current)
        battle_state = BATTLE_STATES.get(telegram_id, battle_identity(current), now)
        spore_cooldown_seconds = (
            SPORE_STRIKE_COOLDOWN_SECONDS
            * companion_effects["skill_cooldown_multiplier"]
        )
        spore_ready = (
            spore_last_used_at <= 0
            or spore_elapsed >= spore_cooldown_seconds
        )

        manual_spore = requested_skill == "spore_strike"
        auto_spore = (
            not requested_skill
            and bool(int(current.get("skills_auto_enabled", 0)))
            and spore_unlocked
            and spore_ready
        )

        if manual_spore and not spore_unlocked:
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                skill_used=None,
                message=unavailable_skill_message(
                    spore_state,
                    "Spore Strike",
                ),
            )

        if manual_spore and not spore_ready:
            remaining = max(
                0.0,
                spore_cooldown_seconds - spore_elapsed,
            )
            connection.commit()
            return build_player_response(
                current,
                attacked=False,
                skill_used=None,
                skill_retry_after=remaining,
                message=f"🍄 Spore Strike: ещё {remaining:.1f} сек.",
            )

        use_spore_strike = manual_spore or auto_spore
        owl_repeat_stacks = 0
        owl_repeat_bonus = 0.0
        if use_spore_strike:
            owl_repeat_stacks, owl_repeat_bonus = BATTLE_STATES.use_skill(
                battle_state, "spore_strike", owl_is_active(current)
            )
        damage_multiplier = (
            float(SKILL_EFFECTS["spore_strike"]["damage_multiplier"])
            * skill_power_multiplier(spore_state["level"])
            * (1.0 + owl_repeat_bonus)
            if use_spore_strike
            else 1.0
        )

        poison_last_used_at = float(
            current.get("poison_cloud_last_used_at", 0)
        )
        poison_until = float(
            current.get("poison_cloud_until", 0)
        )
        poison_next_tick_at = float(
            current.get("poison_cloud_next_tick_at", 0)
        )
        poison_active = (
            poison_until > now
            and poison_next_tick_at > 0
        )
        poison_cooldown_seconds = (
            POISON_CLOUD_COOLDOWN_SECONDS
            * companion_effects["skill_cooldown_multiplier"]
        )
        poison_ready = (
            poison_last_used_at <= 0
            or now - poison_last_used_at
                >= poison_cooldown_seconds
        )
        poison_state = get_player_skill_state(
            current,
            "poison_cloud",
        )
        poison_unlocked = poison_state["available"]
        poison_auto_used = (
            not requested_skill
            and bool(int(current.get("skills_auto_enabled", 0)))
            and poison_unlocked
            and poison_ready
            and not poison_active
        )

        if poison_auto_used:
            poison_last_used_at = now
            poison_until = now + POISON_CLOUD_DURATION_SECONDS
            poison_next_tick_at = now + POISON_CLOUD_TICK_SECONDS
            poison_active = True
            BATTLE_STATES.begin_poison(
                battle_state, owl_is_active(current)
            )

        poison_ticks = 0
        poison_damage = 0

        if poison_next_tick_at > 0:
            tick_limit = min(now, poison_until)
            if tick_limit >= poison_next_tick_at:
                poison_ticks = (
                    int(
                        (tick_limit - poison_next_tick_at)
                        // POISON_CLOUD_TICK_SECONDS
                    )
                    + 1
                )
                poison_ticks = max(0, min(5, poison_ticks))
                poison_next_tick_at += (
                    poison_ticks * POISON_CLOUD_TICK_SECONDS
                )

                poison_damage_per_tick = max(
                    1,
                    round(
                        int(current["damage"])
                        * POISON_CLOUD_DAMAGE_MULTIPLIER
                        * skill_power_multiplier(
                            poison_state["level"]
                        )
                        * calculate_companion_effects(current)[
                            "damage_multiplier"
                        ]
                        * (1.0 + battle_state.poison_owl_bonus)
                        * (1.0 + battle_state.poison_stack_bonus)
                    ),
                )
                poison_damage = (
                    poison_damage_per_tick * poison_ticks
                )
                BATTLE_STATES.record_poison_ticks(battle_state, poison_ticks)

            if now >= poison_until:
                poison_until = 0.0
                poison_next_tick_at = 0.0

        stage = clamp_int(current["stage"], 1, MAX_STAGE)
        boss_active = bool(int(current.get("boss_active", 0)))
        combat_context = CombatContext(
            current_hp=int(current["hero_hp"]),
            max_hp=int(current["hero_max_hp"]),
            enemy_hp=int(current["enemy_hp"]),
            enemy_type="boss" if boss_active else "normal",
            elapsed_time=max(0.0, elapsed),
            active_shield=int(current.get("mushroom_shield_amount", 0)),
            cooldowns={
                "spore_strike": max(0.0, spore_cooldown_seconds - spore_elapsed),
                "poison_cloud": max(0.0, poison_cooldown_seconds - (now - poison_last_used_at)),
            },
        )
        attack_payload = {"damage_multiplier": damage_multiplier}
        COMBAT_EFFECT_ENGINE.dispatch(
            CombatEvent.BEFORE_SKILL if use_spore_strike else CombatEvent.BEFORE_NORMAL_ATTACK,
            combat_context,
            attack_payload,
        )
        dealt_damage, critical = calculate_hero_attack_damage(
            current,
            damage_multiplier=(
                float(attack_payload["damage_multiplier"])
                * companion_effects["damage_multiplier"]
            ),
        )
        companion_damage = (
            int(companion_effects["extra_attack_damage"])
            if not use_spore_strike
            else 0
        )
        if critical and poison_damage:
            equipment_stats = calculate_equipment_stats(
                parse_json_object(current["equipment_json"]),
                int(current["level"]),
            )["total"]
            poison_damage = max(
                1,
                round(
                    poison_damage
                    * (
                        float(equipment_stats["crit_damage"])
                        + float(companion_effects["crit_damage_bonus"])
                    )
                    / 100
                ),
            )
        dealt_damage += poison_damage + companion_damage
        damage_payload = {"damage": dealt_damage, "critical": critical}
        COMBAT_EFFECT_ENGINE.dispatch(
            CombatEvent.AFTER_SKILL if use_spore_strike else CombatEvent.AFTER_NORMAL_ATTACK,
            combat_context,
            damage_payload,
        )
        dealt_damage = max(0, round(float(damage_payload["damage"])))
        combat_context.damage_breakdown["total"] = dealt_damage
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
        companion_healing = 0
        requested_healing = 0
        healing_overflow = 0
        hp_before_healing = max(
            0,
            min(
                max(1, round(int(current["hero_max_hp"]) * float(companion_effects["hp_multiplier"]))),
                round(int(current["hero_hp"]) * float(companion_effects["hp_multiplier"])),
            ),
        )
        if enemy_defeated:
            COMBAT_EFFECT_ENGINE.dispatch(
                CombatEvent.BOSS_KILLED if boss_active else CombatEvent.ENEMY_KILLED,
                combat_context,
                {"damage": dealt_damage},
            )
            hp_multiplier = float(companion_effects["hp_multiplier"])
            effective_max_hp = max(
                1, round(int(current["hero_max_hp"]) * hp_multiplier)
            )
            effective_hp = min(
                effective_max_hp,
                max(0, round(int(current["hero_hp"]) * hp_multiplier)),
            )
            requested_healing = round(
                effective_max_hp
                * float(companion_effects["victory_healing_ratio"])
                * (2 if boss_active else 1)
            )
            companion_healing = max(
                0, min(requested_healing, effective_max_hp - effective_hp)
            )
            healing_overflow = max(0, requested_healing - companion_healing)
            if companion_healing:
                healed_effective_hp = effective_hp + companion_healing
                current["hero_hp"] = min(
                    int(current["hero_max_hp"]),
                    round(healed_effective_hp / hp_multiplier),
                )
                actual_effective_hp = min(
                    effective_max_hp,
                    max(0, round(int(current["hero_hp"]) * hp_multiplier)),
                )
                companion_healing = max(0, actual_effective_hp - effective_hp)
                healing_overflow = max(0, requested_healing - companion_healing)
            total_kills += 1
            experience_reward = 0
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
                    last_enemy_attack_at = now - max(0.0, max(MIN_ENEMY_ATTACK_INTERVAL, 1 / ENEMY_ATTACK_SPEED) - (0.55 if boss_active else 0.85))
                    stage_completed = True
            else:
                next_wave = kills + 1
                chest_reward = (
                    3 if next_wave >= ENEMIES_PER_STAGE else 2
                )
                chests += chest_reward
                kills += 1
                if kills >= ENEMIES_PER_STAGE:
                    kills = ENEMIES_PER_STAGE
                    boss_active = True
                    enemy_max_hp = calculate_enemy_hp(stage, boss=True)
                    enemy_hp = enemy_max_hp
                else:
                    enemy_max_hp = calculate_enemy_hp(stage)
                    enemy_hp = enemy_max_hp
                last_enemy_attack_at = now - max(0.0, max(MIN_ENEMY_ATTACK_INTERVAL, 1 / ENEMY_ATTACK_SPEED) - (0.55 if boss_active else 0.85))
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
                hero_hp = ?,
                stage = ?, kills_in_stage = ?,
                total_kills = ?, total_bosses = ?,
                chests = ?, experience = ?, level = ?,
                highest_stage = ?, boss_active = ?,
                boss_waiting = ?, game_completed = ?,
                progress_reached_at = ?,
                last_attack_at = ?, last_enemy_attack_at = ?,
                spore_strike_last_used_at = ?,
                poison_cloud_last_used_at = ?,
                poison_cloud_until = ?,
                poison_cloud_next_tick_at = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                max(0, enemy_hp),
                enemy_max_hp,
                int(current["hero_hp"]),
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
                (
                    now
                    if use_spore_strike
                    else spore_last_used_at
                ),
                poison_last_used_at,
                0.0 if enemy_defeated else poison_until,
                0.0 if enemy_defeated else poison_next_tick_at,
                int(now),
                telegram_id,
            ),
        )
        if enemy_defeated:
            connection.execute(
                """
                UPDATE players
                SET daily_kills = daily_kills + ?,
                    daily_bosses = daily_bosses + ?
                WHERE telegram_id = ?
                """,
                (
                    0 if boss_defeated else 1,
                    1 if boss_defeated else 0,
                    telegram_id,
                ),
            )

        if enemy_defeated:
            # A spawned enemy is a hard battle boundary. Cooldowns currently reset
            # by design, together with every temporary/persisted combat effect.
            connection.execute(
                """
                UPDATE players
                SET mushroom_shield_amount = 0,
                    mushroom_shield_last_used_at = 0,
                    spore_strike_last_used_at = 0,
                    poison_cloud_last_used_at = 0,
                    poison_cloud_until = 0,
                    poison_cloud_next_tick_at = 0
                WHERE telegram_id = ?
                """,
                (telegram_id,),
            )
            BATTLE_STATES.reset(telegram_id)

        sync_player_stats(connection, telegram_id)
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()
    message = f"⚔️ Нанесено {dealt_damage} урона"
    if use_spore_strike:
        message = f"🍄 Spore Strike: {dealt_damage} урона"
    if critical:
        message = (
            f"🍄💥 Spore Strike — крит: {dealt_damage}"
            if use_spore_strike
            else f"💥 Критический удар: {dealt_damage}"
        )
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
    if companion_healing:
        message += f" · 🌿 Лечение спутника: +{companion_healing} HP"
    persisted_sequence = stage_sequence_state(updated)
    hp_after_healing = persisted_sequence["current_hp"]
    sequence = stage_sequence_state(
        updated,
        hp_before_healing=hp_before_healing,
        hp_after_healing=hp_after_healing,
        hp_carried_to_next_battle=hp_after_healing,
        companion_healing=companion_healing,
        healing_overflow=healing_overflow,
        temporary_effects_cleared=enemy_defeated,
        cooldowns_reset_between_battles=enemy_defeated,
    )
    return build_player_response(
        updated,
        attacked=True,
        damage_dealt=dealt_damage,
        companion_damage=companion_damage,
        companion_healing=companion_healing,
        healing_overflow=healing_overflow,
        stage_sequence=sequence,
        critical=critical,
        skill_used=("spore_strike" if use_spore_strike else None),
        spore_strike_used=use_spore_strike,
        poison_cloud_used=poison_auto_used,
        poison_ticks=poison_ticks,
        poison_damage=poison_damage,
        enemy_defeated=enemy_defeated,
        boss_defeated=boss_defeated,
        stage_completed=stage_completed,
        chest_reward=chest_reward,
        experience_reward=experience_reward,
        reward=0,
        message=message,
        owl_repeat_stacks=owl_repeat_stacks,
        owl_repeat_bonus=round(owl_repeat_bonus, 4),
    )


@app.post("/skills/poison-cloud/use")
def use_poison_cloud(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()
    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        if int(current.get("hero_hp", 0)) <= 0:
            connection.commit()
            return build_player_response(
                current,
                poison_used=False,
                message="💀 Герой ожидает возрождения",
            )

        poison_state = get_player_skill_state(
            current,
            "poison_cloud",
        )
        if not poison_state["available"]:
            connection.commit()
            return build_player_response(
                current,
                poison_used=False,
                message=unavailable_skill_message(
                    poison_state,
                    "Poison Cloud",
                ),
            )

        last_used_at = float(
            current.get("poison_cloud_last_used_at", 0)
        )
        elapsed = now - last_used_at
        cooldown_seconds = companion_skill_cooldown(
            current, POISON_CLOUD_COOLDOWN_SECONDS
        )

        if (
            last_used_at > 0
            and elapsed < cooldown_seconds
        ):
            remaining = max(
                0.0,
                cooldown_seconds - elapsed,
            )
            connection.commit()
            return build_player_response(
                current,
                poison_used=False,
                skill_retry_after=remaining,
                message=f"☁️ Poison Cloud: ещё {remaining:.1f} сек.",
            )

        poison_until = now + POISON_CLOUD_DURATION_SECONDS
        poison_next_tick_at = now + POISON_CLOUD_TICK_SECONDS
        battle_state = BATTLE_STATES.get(telegram_id, battle_identity(current), now)
        owl_repeat_stacks, owl_repeat_bonus, poison_completion_stacks, poison_stack_bonus = (
            BATTLE_STATES.begin_poison(battle_state, owl_is_active(current))
        )

        connection.execute(
            """
            UPDATE players
            SET poison_cloud_last_used_at = ?,
                poison_cloud_until = ?,
                poison_cloud_next_tick_at = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                now,
                poison_until,
                poison_next_tick_at,
                int(now),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    return build_player_response(
        updated,
        poison_used=True,
        skill_used="poison_cloud",
        owl_repeat_stacks=owl_repeat_stacks,
        owl_repeat_bonus=round(owl_repeat_bonus, 4),
        poison_completion_stacks=poison_completion_stacks,
        poison_stack_bonus=round(poison_stack_bonus, 4),
        message="☁️ Poison Cloud активировано на 5 секунд",
    )


@app.post("/skills/mushroom-shield/use")
def use_mushroom_shield(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()
    connection = get_database()
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        if int(current.get("hero_hp", 0)) <= 0:
            connection.commit()
            return build_player_response(
                current,
                shield_used=False,
                message="💀 Герой ожидает возрождения",
            )

        shield_state = get_player_skill_state(
            current,
            "mushroom_shield",
        )
        if not shield_state["available"]:
            connection.commit()
            return build_player_response(
                current,
                shield_used=False,
                message=unavailable_skill_message(
                    shield_state,
                    "Mushroom Shield",
                ),
            )

        last_used_at = float(
            current.get("mushroom_shield_last_used_at", 0)
        )
        elapsed = now - last_used_at
        cooldown_seconds = companion_skill_cooldown(
            current, MUSHROOM_SHIELD_COOLDOWN_SECONDS
        )
        if (
            last_used_at > 0
            and elapsed < cooldown_seconds
        ):
            remaining = max(
                0.0,
                cooldown_seconds - elapsed,
            )
            connection.commit()
            return build_player_response(
                current,
                shield_used=False,
                skill_retry_after=remaining,
                message=f"🛡️ Mushroom Shield: ещё {remaining:.1f} сек.",
            )

        battle_state = BATTLE_STATES.get(telegram_id, battle_identity(current), now)
        owl_repeat_stacks, owl_repeat_bonus = BATTLE_STATES.begin_shield(
            battle_state, owl_is_active(current)
        )
        shield_capacity = max(
            1,
            round(
                int(current["hero_max_hp"])
                * calculate_companion_effects(current)["hp_multiplier"]
                * MUSHROOM_SHIELD_HP_RATIO
                * skill_power_multiplier(
                    shield_state["level"]
                )
                * (
                    BATTLE_STATES.peek(telegram_id, battle_identity(current), now).shield_capacity_multiplier
                    if BATTLE_STATES.peek(telegram_id, battle_identity(current), now) is not None
                    else 1.0
                )
            ),
        )
        connection.execute(
            """
            UPDATE players
            SET mushroom_shield_amount = ?,
                mushroom_shield_last_used_at = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (shield_capacity, now, int(now), telegram_id),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    return build_player_response(
        updated,
        shield_used=True,
        shield_gained=shield_capacity,
        owl_repeat_stacks=owl_repeat_stacks,
        owl_repeat_bonus=round(owl_repeat_bonus, 4),
        skill_used="mushroom_shield",
        message=f"🛡️ Mushroom Shield: {shield_capacity} защиты",
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

        hp_multiplier = float(
            calculate_companion_effects(current)["hp_multiplier"]
        )
        base_hero_max_hp = max(1, int(current["hero_max_hp"]))
        hero_max_hp = max(1, round(base_hero_max_hp * hp_multiplier))
        hero_hp_before = max(
            0,
            min(
                hero_max_hp,
                round(int(current["hero_hp"]) * hp_multiplier),
            ),
        )
        shield_state = get_player_skill_state(
            current,
            "mushroom_shield",
        )
        base_shield_capacity = max(
            1,
            round(
                hero_max_hp
                * MUSHROOM_SHIELD_HP_RATIO
                * skill_power_multiplier(
                    shield_state["level"]
                )
            ),
        )
        battle_state = BATTLE_STATES.get(telegram_id, battle_identity(current), now)
        shield_capacity = max(
            1, round(base_shield_capacity * battle_state.shield_capacity_multiplier)
        )
        shield_amount = max(
            0,
            min(
                shield_capacity,
                int(current.get("mushroom_shield_amount", 0)),
            ),
        )
        shield_last_used_at = float(
            current.get("mushroom_shield_last_used_at", 0)
        )
        shield_elapsed = now - shield_last_used_at
        shield_cooldown_seconds = companion_skill_cooldown(
            current, MUSHROOM_SHIELD_COOLDOWN_SECONDS
        )
        shield_ready = (
            shield_last_used_at <= 0
            or shield_elapsed >= shield_cooldown_seconds
        )
        shield_unlocked = shield_state["available"]
        shield_auto_used = (
            bool(int(current.get("skills_auto_enabled", 0)))
            and shield_unlocked
            and shield_ready
            and hero_hp_before / hero_max_hp
                <= MUSHROOM_SHIELD_AUTO_HP_RATIO
            and shield_amount < shield_capacity
        )

        if shield_auto_used:
            _, auto_shield_bonus = BATTLE_STATES.begin_shield(
                battle_state, owl_is_active(current)
            )
            shield_capacity = max(1, round(base_shield_capacity * (1.0 + auto_shield_bonus)))
            shield_amount = shield_capacity
            shield_last_used_at = now

        raw_incoming_damage = calculate_enemy_damage(
            stage,
            boss=boss_active,
        )
        mitigation_remaining = BATTLE_STATES.mitigation_remaining(battle_state, now)
        incoming_damage = round(
            raw_incoming_damage
            * BATTLE_STATES.incoming_damage_multiplier(battle_state, now)
        )
        damage_reduced_by_effect = raw_incoming_damage - incoming_damage
        shield_before_damage = shield_amount
        absorbed_damage = min(shield_amount, incoming_damage)
        received_damage = max(0, incoming_damage - absorbed_damage)
        shield_amount -= absorbed_damage
        shield_broken = shield_before_damage > 0 and shield_amount == 0
        if shield_broken:
            BATTLE_STATES.break_shield(battle_state, now)
            mitigation_remaining = 3.0

        effective_hero_hp = max(0, hero_hp_before - received_damage)
        hero_defeated = effective_hero_hp <= 0
        hero_hp = (
            0
            if hero_defeated
            else min(
                base_hero_max_hp,
                round(effective_hero_hp / hp_multiplier),
            )
        )
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
        if hero_defeated:
            BATTLE_STATES.reset(telegram_id)
        connection.execute(
            """
            UPDATE players
            SET hero_hp = ?, defeats = ?,
                boss_active = ?, boss_waiting = ?,
                enemy_hp = ?, enemy_max_hp = ?,
                mushroom_shield_amount = ?,
                mushroom_shield_last_used_at = ?,
                poison_cloud_until = ?,
                poison_cloud_next_tick_at = ?,
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
                0 if hero_defeated else shield_amount,
                shield_last_used_at,
                0.0 if hero_defeated else float(current.get("poison_cloud_until", 0)),
                0.0 if hero_defeated else float(current.get("poison_cloud_next_tick_at", 0)),
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
    if absorbed_damage > 0:
        message = (
            f"🛡️ Щит поглотил {absorbed_damage}, "
            f"получено {received_damage} урона"
        )
    if shield_auto_used:
        message = (
            f"🛡️ AUTO активировал щит. "
            f"Поглощено {absorbed_damage}, "
            f"получено {received_damage}"
        )
    if hero_defeated and boss_active:
        message = "💀 Босс победил. После возрождения нажмите «Босс»"
    elif hero_defeated:
        message = "💀 Герой повержен"
    return build_player_response(
        updated,
        enemy_attacked=True,
        hero_defeated=hero_defeated,
        incoming_damage=incoming_damage,
        raw_incoming_damage=raw_incoming_damage,
        damage_reduced_by_effect=damage_reduced_by_effect,
        shield_mitigation_active=(not hero_defeated and mitigation_remaining > 0),
        shield_mitigation_remaining=(0.0 if hero_defeated else round(mitigation_remaining, 3)),
        received_damage=received_damage,
        absorbed_damage=absorbed_damage,
        shield_remaining=shield_amount,
        shield_auto_used=shield_auto_used,
        skill_used=(
            "mushroom_shield"
            if shield_auto_used
            else None
        ),
        message=message,
    )


@app.post("/respawn")
def respawn(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    BATTLE_STATES.reset(telegram_id)
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
                mushroom_shield_amount = 0,
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
    BATTLE_STATES.reset(telegram_id)
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



@app.get("/challenge/chest-boss")
def get_chest_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        ensure_chest_boss_state(
            connection,
            telegram_id,
            time.time(),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    return {
        "chest_boss": build_chest_boss_state(updated),
    }


@app.post("/challenge/chest-boss/start")
def start_chest_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        ensure_chest_boss_state(
            connection,
            telegram_id,
            now,
        )

        current = load_player(connection, telegram_id)

        if bool(int(current.get("chest_boss_active", 0))):
            connection.commit()
            return build_player_response(
                current,
                chest_boss_started=False,
                message="Бой с Хранителем уже идёт",
            )

        attempt_type = consume_chest_boss_attempt(
            connection,
            current,
        )

        if attempt_type is None:
            connection.commit()
            return build_player_response(
                current,
                chest_boss_started=False,
                no_attempts=True,
                message="Попытки закончились",
            )

        level = clamp_int(
            current.get("chest_boss_level", 1),
            1,
            CHEST_BOSS_MAX_LEVEL,
        )

        boss_hp = calculate_chest_boss_hp(level)
        hero_hp = max(1, int(current["hero_max_hp"]))

        connection.execute(
            """
            UPDATE players
            SET chest_boss_active = 1,
                chest_boss_hp = ?,
                chest_boss_max_hp = ?,
                chest_boss_hero_hp = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                boss_hp,
                boss_hp,
                hero_hp,
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return build_player_response(
        updated,
        chest_boss_started=True,
        attempt_type=attempt_type,
        message=(
            f"🧰 Хранитель сундуков — уровень {level}. "
            f"Награда: {calculate_chest_boss_reward(level)} сундуков"
        ),
    )


@app.post("/challenge/chest-boss/attack")
def attack_chest_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        ensure_chest_boss_state(
            connection,
            telegram_id,
            now,
        )

        current = load_player(connection, telegram_id)

        if not bool(int(current.get("chest_boss_active", 0))):
            connection.commit()
            return build_player_response(
                current,
                chest_boss_attacked=False,
                message="Сначала начните испытание",
            )

        level = clamp_int(
            current.get("chest_boss_level", 1),
            1,
            CHEST_BOSS_MAX_LEVEL,
        )

        boss_hp = max(
            0,
            int(current.get("chest_boss_hp", 0)),
        )
        hero_hp = max(
            0,
            int(current.get("chest_boss_hero_hp", 0)),
        )

        hero_damage = max(1, int(current.get("damage", 1)))
        boss_damage = calculate_chest_boss_damage(level)

        crit_chance = max(
            0.0,
            min(
                1.0,
                float(
                    calculate_equipment_stats(
                        public_equipment(current),
                        int(current.get("level", 1)),
                    )["total"]["crit_chance"]
                )
                / 100,
            ),
        )

        crit_damage_percent = max(
            100,
            int(
                calculate_equipment_stats(
                    public_equipment(current),
                    int(current.get("level", 1)),
                )["total"]["crit_damage"]
            ),
        )

        critical = random.random() < crit_chance

        outgoing_damage = hero_damage

        if critical:
            outgoing_damage = max(
                1,
                round(
                    hero_damage
                    * crit_damage_percent
                    / 100
                ),
            )

        boss_hp = max(0, boss_hp - outgoing_damage)

        if boss_hp <= 0:
            chest_reward = calculate_chest_boss_reward(level)
            next_level = min(
                CHEST_BOSS_MAX_LEVEL,
                level + 1,
            )

            connection.execute(
                """
                UPDATE players
                SET chest_boss_active = 0,
                    chest_boss_hp = 0,
                    chest_boss_hero_hp = 0,
                    chest_boss_level = ?,
                    chests = chests + ?,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    next_level,
                    chest_reward,
                    int(now),
                    telegram_id,
                ),
            )

            connection.commit()
            updated = load_player(connection, telegram_id)

            return build_player_response(
                updated,
                chest_boss_attacked=True,
                chest_boss_victory=True,
                critical=critical,
                outgoing_damage=outgoing_damage,
                chest_reward=chest_reward,
                defeated_level=level,
                next_level=next_level,
                message=(
                    f"🏆 Хранитель побеждён! "
                    f"Получено сундуков: {chest_reward}"
                ),
            )

        hero_hp = max(0, hero_hp - boss_damage)

        if hero_hp <= 0:
            connection.execute(
                """
                UPDATE players
                SET chest_boss_active = 0,
                    chest_boss_hp = 0,
                    chest_boss_hero_hp = 0,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    int(now),
                    telegram_id,
                ),
            )

            connection.commit()
            updated = load_player(connection, telegram_id)

            return build_player_response(
                updated,
                chest_boss_attacked=True,
                chest_boss_defeat=True,
                critical=critical,
                outgoing_damage=outgoing_damage,
                incoming_damage=boss_damage,
                message=(
                    "💀 Хранитель сундуков победил. "
                    "Попытка потрачена"
                ),
            )

        connection.execute(
            """
            UPDATE players
            SET chest_boss_hp = ?,
                chest_boss_hero_hp = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                boss_hp,
                hero_hp,
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return build_player_response(
        updated,
        chest_boss_attacked=True,
        critical=critical,
        outgoing_damage=outgoing_damage,
        incoming_damage=boss_damage,
        message=(
            f"⚔️ Нанесено {outgoing_damage}. "
            f"Получено {boss_damage} урона"
        ),
    )


@app.post("/challenge/chest-boss/ad-attempt")
def grant_chest_boss_ad_attempt(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        ensure_chest_boss_state(
            connection,
            telegram_id,
            now,
        )

        current = load_player(connection, telegram_id)

        ad_used = max(
            0,
            int(current.get("chest_boss_ad_attempts_used", 0)),
        )

        if ad_used >= CHEST_BOSS_AD_ATTEMPTS:
            connection.commit()
            return build_player_response(
                current,
                ad_attempt_granted=False,
                message="Сегодня рекламные попытки закончились",
            )

        connection.execute(
            """
            UPDATE players
            SET chest_boss_ad_attempts_used =
                    chest_boss_ad_attempts_used + 1,
                chest_boss_bonus_attempts =
                    chest_boss_bonus_attempts + 1,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return build_player_response(
        updated,
        ad_attempt_granted=True,
        message="📺 Получена дополнительная попытка",
    )


@app.post("/challenge/chest-boss/use-key")
def use_chest_boss_key(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        keys = max(
            0,
            int(current.get("chest_boss_keys", 0)),
        )

        if keys <= 0:
            connection.commit()
            return build_player_response(
                current,
                key_used=False,
                message="Ключей испытания нет",
            )

        connection.execute(
            """
            UPDATE players
            SET chest_boss_keys = chest_boss_keys - 1,
                chest_boss_bonus_attempts =
                    chest_boss_bonus_attempts + 1,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return build_player_response(
        updated,
        key_used=True,
        message="🔑 Ключ использован. Получена попытка",
    )


@app.post("/challenge/chest-boss/leave")
def leave_chest_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        connection.execute(
            """
            UPDATE players
            SET chest_boss_active = 0,
                chest_boss_hp = 0,
                chest_boss_max_hp = 0,
                chest_boss_hero_hp = 0,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
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
        chest_boss_left=True,
        message="Испытание завершено. Попытка не возвращена",
    )




@app.get("/challenge/gem-boss")
def get_gem_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        ensure_gem_boss_state(
            connection,
            telegram_id,
            time.time(),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return {
        "gem_boss": build_gem_boss_state(updated),
        "gems": max(0, int(updated.get("gems", 0))),
    }


@app.post("/challenge/gem-boss/start")
def start_gem_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        ensure_gem_boss_state(
            connection,
            telegram_id,
            now,
        )

        current = load_player(connection, telegram_id)

        if bool(int(current.get("gem_boss_active", 0))):
            connection.commit()

            return build_player_response(
                current,
                gem_boss_started=False,
                message="Бой со Стражем уже идёт",
            )

        if not consume_gem_boss_attempt(
            connection,
            current,
        ):
            connection.commit()

            return build_player_response(
                current,
                gem_boss_started=False,
                no_attempts=True,
                message="Попытки Стража закончились",
            )

        level = clamp_int(
            current.get("gem_boss_level", 1),
            1,
            GEM_BOSS_MAX_LEVEL,
        )

        boss_hp = calculate_gem_boss_hp(level)
        hero_hp = max(
            1,
            int(current.get("hero_max_hp", 1)),
        )

        connection.execute(
            """
            UPDATE players
            SET gem_boss_active = 1,
                gem_boss_hp = ?,
                gem_boss_max_hp = ?,
                gem_boss_hero_hp = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                boss_hp,
                boss_hp,
                hero_hp,
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    reward = calculate_gem_boss_reward(level)

    return build_player_response(
        updated,
        gem_boss_started=True,
        message=(
            f"💎 Страж самоцветов — уровень {level}. "
            f"Награда: {reward} самоцветов"
        ),
    )


@app.post("/challenge/gem-boss/attack")
def attack_gem_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        ensure_gem_boss_state(
            connection,
            telegram_id,
            now,
        )

        current = load_player(connection, telegram_id)

        if not bool(int(current.get("gem_boss_active", 0))):
            connection.commit()

            return build_player_response(
                current,
                gem_boss_attacked=False,
                message="Сначала начните испытание Стража",
            )

        level = clamp_int(
            current.get("gem_boss_level", 1),
            1,
            GEM_BOSS_MAX_LEVEL,
        )

        boss_hp = max(
            0,
            int(current.get("gem_boss_hp", 0)),
        )
        hero_hp = max(
            0,
            int(current.get("gem_boss_hero_hp", 0)),
        )

        outgoing_damage, critical = (
            calculate_hero_attack_damage(current)
        )

        boss_damage = calculate_gem_boss_damage(level)
        boss_hp = max(0, boss_hp - outgoing_damage)

        if boss_hp <= 0:
            gem_reward = calculate_gem_boss_reward(level)
            next_level = min(
                GEM_BOSS_MAX_LEVEL,
                level + 1,
            )

            connection.execute(
                """
                UPDATE players
                SET gem_boss_active = 0,
                    gem_boss_hp = 0,
                    gem_boss_max_hp = 0,
                    gem_boss_hero_hp = 0,
                    gem_boss_level = ?,
                    gems = gems + ?,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    next_level,
                    gem_reward,
                    int(now),
                    telegram_id,
                ),
            )

            connection.commit()
            updated = load_player(connection, telegram_id)

            return build_player_response(
                updated,
                gem_boss_attacked=True,
                gem_boss_victory=True,
                critical=critical,
                outgoing_damage=outgoing_damage,
                gem_reward=gem_reward,
                defeated_level=level,
                next_level=next_level,
                message=(
                    f"🏆 Страж самоцветов побеждён! "
                    f"Получено: {gem_reward} 💎"
                ),
            )

        hero_hp = max(0, hero_hp - boss_damage)

        if hero_hp <= 0:
            connection.execute(
                """
                UPDATE players
                SET gem_boss_active = 0,
                    gem_boss_hp = 0,
                    gem_boss_max_hp = 0,
                    gem_boss_hero_hp = 0,
                    updated_at = ?
                WHERE telegram_id = ?
                """,
                (
                    int(now),
                    telegram_id,
                ),
            )

            connection.commit()
            updated = load_player(connection, telegram_id)

            return build_player_response(
                updated,
                gem_boss_attacked=True,
                gem_boss_defeat=True,
                critical=critical,
                outgoing_damage=outgoing_damage,
                incoming_damage=boss_damage,
                message=(
                    "💀 Страж самоцветов победил. "
                    "Попытка потрачена"
                ),
            )

        connection.execute(
            """
            UPDATE players
            SET gem_boss_hp = ?,
                gem_boss_hero_hp = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                boss_hp,
                hero_hp,
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return build_player_response(
        updated,
        gem_boss_attacked=True,
        critical=critical,
        outgoing_damage=outgoing_damage,
        incoming_damage=boss_damage,
        message=(
            f"⚔️ Нанесено {outgoing_damage}. "
            f"Получено {boss_damage} урона"
        ),
    )



@app.post("/challenge/gem-boss/ad-attempt")
def grant_gem_boss_ad_attempt(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        ensure_gem_boss_state(
            connection,
            telegram_id,
            now,
        )

        current = load_player(connection, telegram_id)

        ad_used = max(
            0,
            int(current.get("gem_boss_ad_attempts_used", 0)),
        )

        if ad_used >= GEM_BOSS_AD_ATTEMPTS:
            connection.commit()

            return build_player_response(
                current,
                gem_boss_ad_attempt_granted=False,
                message="Сегодня рекламные попытки закончились",
            )

        connection.execute(
            """
            UPDATE players
            SET gem_boss_ad_attempts_used =
                    gem_boss_ad_attempts_used + 1,
                gem_boss_bonus_attempts =
                    gem_boss_bonus_attempts + 1,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return build_player_response(
        updated,
        gem_boss_ad_attempt_granted=True,
        message="📺 Получена дополнительная попытка",
    )


@app.post("/challenge/gem-boss/use-key")
def use_gem_boss_key(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    now = time.time()

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        keys = max(
            0,
            int(current.get("gem_boss_keys", 0)),
        )

        if keys <= 0:
            connection.commit()

            return build_player_response(
                current,
                gem_boss_key_used=False,
                message="Ключей Стража нет",
            )

        connection.execute(
            """
            UPDATE players
            SET gem_boss_keys = gem_boss_keys - 1,
                gem_boss_bonus_attempts =
                    gem_boss_bonus_attempts + 1,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                int(now),
                telegram_id,
            ),
        )

        connection.commit()
        updated = load_player(connection, telegram_id)

    finally:
        connection.close()

    return build_player_response(
        updated,
        gem_boss_key_used=True,
        message="🔑 Ключ использован. Получена попытка",
    )


@app.post("/challenge/gem-boss/leave")
def leave_gem_boss(
    x_telegram_init_data: str = Header(...),
) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])

    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")

        connection.execute(
            """
            UPDATE players
            SET gem_boss_active = 0,
                gem_boss_hp = 0,
                gem_boss_max_hp = 0,
                gem_boss_hero_hp = 0,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
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
        gem_boss_left=True,
        message=(
            "Испытание завершено. "
            "Попытка не возвращена"
        ),
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

    chest_experience_reward = chest_exp_reward(
        int(current["chest_level"])
    )
    total_exp = (
        max(0, int(current.get("experience", 0)))
        + chest_experience_reward
    )
    previous_level = max(1, int(current.get("level", 1)))
    new_level = max(
        previous_level,
        level_from_total_exp(total_exp),
    )
    if auto_mode and not comparison["is_improvement"]:
        sell_price = max(0, int(loot.get("sell_price", 0)))
        connection.execute(
            """
            UPDATE players
            SET chests = chests - 1,
                daily_chests_opened = daily_chests_opened + 1,
                gold = gold + ?,
                experience = ?,
                level = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                sell_price,
                total_exp,
                new_level,
                now,
                telegram_id,
            ),
        )
        sync_player_stats(connection, telegram_id)
        updated = load_player(connection, telegram_id)
        return updated, {
            "opened": True,
            "auto_sold": True,
            "paused": False,
            "loot": loot,
            "comparison": comparison,
            "sell_price": sell_price,
            "experience_reward": chest_experience_reward,
            "level_up": new_level > previous_level,
            "message": f"💰 {loot['name']} продан за {sell_price}",
        }
    connection.execute(
        """
        UPDATE players
        SET chests = chests - 1,
            daily_chests_opened = daily_chests_opened + 1,
            pending_loot_json = ?,
            experience = ?,
            level = ?,
            updated_at = ?
        WHERE telegram_id = ?
        """,
        (
            json.dumps(loot, ensure_ascii=False),
            total_exp,
            new_level,
            now,
            telegram_id,
        ),
    )
    sync_player_stats(connection, telegram_id)
    updated = load_player(connection, telegram_id)
    return updated, {
        "opened": True,
        "auto_sold": False,
        "paused": auto_mode,
        "loot": loot,
        "comparison": comparison,
        "experience_reward": chest_experience_reward,
        "level_up": new_level > previous_level,
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



# SKILL_SLOTS_API_V1
@app.post("/skills/equip")
def equip_skill(
    slot: int,
    skill_id: str,
    x_telegram_init_data: str = Header(...),
) -> dict:
    if slot not in (1, 2, 3):
        raise HTTPException(
            status_code=400,
            detail="Номер слота должен быть от 1 до 3",
        )

    skill_id = str(skill_id or "").strip()
    definition = SKILL_CATALOG.get(skill_id)

    if definition is None:
        raise HTTPException(
            status_code=404,
            detail="Навык не найден",
        )

    if not bool(definition.get("implemented")):
        raise HTTPException(
            status_code=409,
            detail="Механика этого навыка пока не реализована",
        )

    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        level = max(1, int(current.get("level", 1)))
        required_level = int(SKILL_SLOT_UNLOCK_LEVELS[slot - 1])

        if level < required_level:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Слот {slot} откроется "
                    f"на {required_level} уровне героя"
                ),
            )

        collection = normalize_skill_collection(
            current.get("skills_collection_json")
        )
        entry = collection.get(skill_id)

        if not isinstance(entry, dict) or not bool(entry.get("owned")):
            raise HTTPException(
                status_code=403,
                detail="Сначала получите этот навык",
            )

        slots = normalize_skill_slots(
            current.get("skill_slots_json"),
            collection,
        )

        # Один навык нельзя установить сразу в два слота.
        slots = [
            None if equipped_id == skill_id else equipped_id
            for equipped_id in slots
        ]
        slots[slot - 1] = skill_id

        now = int(time.time())
        connection.execute(
            """
            UPDATE players
            SET skill_slots_json = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                json.dumps(
                    slots,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    return build_player_response(
        updated,
        skill_equipped=True,
        equipped_skill_id=skill_id,
        equipped_slot=slot,
        message=(
            f"✅ {definition['name']} установлен "
            f"в слот {slot}"
        ),
    )


@app.post("/skills/unequip")
def unequip_skill(
    slot: int,
    x_telegram_init_data: str = Header(...),
) -> dict:
    if slot not in (1, 2, 3):
        raise HTTPException(
            status_code=400,
            detail="Номер слота должен быть от 1 до 3",
        )

    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        collection = normalize_skill_collection(
            current.get("skills_collection_json")
        )
        slots = normalize_skill_slots(
            current.get("skill_slots_json"),
            collection,
        )

        removed_skill_id = slots[slot - 1]
        slots[slot - 1] = None

        now = int(time.time())
        connection.execute(
            """
            UPDATE players
            SET skill_slots_json = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                json.dumps(
                    slots,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    removed_definition = (
        SKILL_CATALOG.get(removed_skill_id)
        if removed_skill_id
        else None
    )
    removed_name = (
        removed_definition.get("name")
        if removed_definition
        else None
    )

    return build_player_response(
        updated,
        skill_unequipped=True,
        unequipped_skill_id=removed_skill_id,
        unequipped_slot=slot,
        message=(
            f"Снят навык {removed_name} из слота {slot}"
            if removed_name
            else f"Слот {slot} уже пуст"
        ),
    )


# COMPANION_SLOTS_API_V1
@app.post("/companions/equip")
def equip_companion(
    slot: int,
    companion_id: str,
    x_telegram_init_data: str = Header(...),
) -> dict:
    if slot not in (1, 2, 3):
        raise HTTPException(
            status_code=400,
            detail="Номер слота должен быть от 1 до 3",
        )

    companion_id = str(companion_id or "").strip()
    definition = COMPANION_CATALOG.get(companion_id)

    if definition is None:
        raise HTTPException(
            status_code=404,
            detail="Спутник не найден",
        )

    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        level = max(1, int(current.get("level", 1)))

        if level < COMPANION_SYSTEM_UNLOCK_LEVEL:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Система спутников откроется "
                    f"на {COMPANION_SYSTEM_UNLOCK_LEVEL} уровне героя"
                ),
            )

        required_level = int(
            COMPANION_SLOT_UNLOCK_LEVELS[slot - 1]
        )

        if level < required_level:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Слот {slot} откроется "
                    f"на {required_level} уровне героя"
                ),
            )

        collection = normalize_companion_collection(
            current.get("companions_collection_json")
        )
        entry = collection.get(companion_id)

        if not isinstance(entry, dict) or not bool(entry.get("owned")):
            raise HTTPException(
                status_code=403,
                detail="Сначала получите этого спутника",
            )

        slots = normalize_companion_slots(
            current.get("companion_slots_json"),
            collection,
        )
        occupied_slot = next(
            (
                index + 1
                for index, equipped_id in enumerate(slots)
                if equipped_id == companion_id
            ),
            None,
        )

        if occupied_slot is not None and occupied_slot != slot:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Этот спутник уже установлен "
                    f"в слот {occupied_slot}"
                ),
            )

        slots[slot - 1] = companion_id
        connection.execute(
            """
            UPDATE players
            SET companion_slots_json = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                json.dumps(
                    slots,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
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
        companion_equipped=True,
        equipped_companion_id=companion_id,
        equipped_companion_slot=slot,
        message=(
            f"✅ {definition['name']} установлен "
            f"в слот {slot}"
        ),
    )


@app.post("/companions/unequip")
def unequip_companion(
    slot: int,
    x_telegram_init_data: str = Header(...),
) -> dict:
    if slot not in (1, 2, 3):
        raise HTTPException(
            status_code=400,
            detail="Номер слота должен быть от 1 до 3",
        )

    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)
        level = max(1, int(current.get("level", 1)))

        if level < COMPANION_SYSTEM_UNLOCK_LEVEL:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Система спутников откроется "
                    f"на {COMPANION_SYSTEM_UNLOCK_LEVEL} уровне героя"
                ),
            )

        required_level = int(
            COMPANION_SLOT_UNLOCK_LEVELS[slot - 1]
        )

        if level < required_level:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Слот {slot} откроется "
                    f"на {required_level} уровне героя"
                ),
            )

        collection = normalize_companion_collection(
            current.get("companions_collection_json")
        )
        slots = normalize_companion_slots(
            current.get("companion_slots_json"),
            collection,
        )
        removed_companion_id = slots[slot - 1]
        slots[slot - 1] = None

        connection.execute(
            """
            UPDATE players
            SET companion_slots_json = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                json.dumps(
                    slots,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                int(time.time()),
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    removed_definition = (
        COMPANION_CATALOG.get(removed_companion_id)
        if removed_companion_id
        else None
    )
    removed_name = (
        removed_definition.get("name")
        if removed_definition
        else None
    )

    return build_player_response(
        updated,
        companion_unequipped=True,
        unequipped_companion_id=removed_companion_id,
        unequipped_companion_slot=slot,
        message=(
            f"Снят спутник {removed_name} из слота {slot}"
            if removed_name
            else f"Слот {slot} уже пуст"
        ),
    )


@app.post("/skills/summon")
def summon_skills(
    count: int = 1,
    x_telegram_init_data: str = Header(...),
) -> dict:
    if count not in (1, 10):
        raise HTTPException(
            status_code=400,
            detail="Доступен призыв только ×1 или ×10",
        )

    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        if int(current.get("level", 1)) < SKILL_SYSTEM_UNLOCK_LEVEL:
            connection.commit()
            return build_player_response(
                current,
                summoned=False,
                summon_results=[],
                message=(
                    f"🔒 Система навыков откроется "
                    f"на {SKILL_SYSTEM_UNLOCK_LEVEL} уровне"
                ),
            )

        cost = SKILL_SUMMON_COST * count
        scrolls = max(
            0,
            int(current.get("skill_scrolls", 0)),
        )

        if scrolls < cost:
            connection.commit()
            return build_player_response(
                current,
                summoned=False,
                summon_results=[],
                summon_cost=cost,
                missing_scrolls=cost - scrolls,
                message=(
                    f"📜 Недостаточно свитков: "
                    f"нужно {cost}, есть {scrolls}"
                ),
            )

        collection = normalize_skill_collection(
            current.get("skills_collection_json")
        )
        summon_exp = max(
            0,
            int(current.get("skill_summon_exp", 0)),
        )
        old_summon_level = skill_summon_level_from_exp(
            summon_exp
        )
        results = []

        for _ in range(count):
            current_summon_level = skill_summon_level_from_exp(
                summon_exp
            )
            skill_id = roll_skill_id(current_summon_level)
            result = apply_skill_summon(
                collection,
                skill_id,
            )
            result["summon_level"] = current_summon_level
            results.append(result)
            summon_exp += 1

        new_summon_level = skill_summon_level_from_exp(
            summon_exp
        )
        remaining_scrolls = scrolls - cost
        now = int(time.time())

        connection.execute(
            """
            UPDATE players
            SET skill_scrolls = ?,
                skill_summon_level = ?,
                skill_summon_exp = ?,
                skills_collection_json = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                remaining_scrolls,
                new_summon_level,
                summon_exp,
                json.dumps(
                    collection,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(connection, telegram_id)
    finally:
        connection.close()

    new_count = sum(
        1
        for result in results
        if result["new"]
    )
    levels_gained = sum(
        int(result["levels_gained"])
        for result in results
    )
    summon_levels_gained = (
        new_summon_level - old_summon_level
    )

    message = f"✨ Открыто свитков: {count}"

    if new_count > 0:
        message += f". Новых навыков: {new_count}"

    if levels_gained > 0:
        message += f". Улучшений навыков: {levels_gained}"

    if summon_levels_gained > 0:
        message += (
            f". Уровень призыва повышен "
            f"до {new_summon_level}"
        )

    return build_player_response(
        updated,
        summoned=True,
        summon_count=count,
        summon_cost=cost,
        summon_results=results,
        new_skills_count=new_count,
        skill_levels_gained=levels_gained,
        summon_level_before=old_summon_level,
        summon_level_after=new_summon_level,
        summon_levels_gained=summon_levels_gained,
        message=message,
    )


# COMPANION_SUMMON_API_V1
@app.post("/companions/summon")
def summon_companions(
    count: int = 1,
    x_telegram_init_data: str = Header(...),
) -> dict:
    if count not in (1, 10):
        raise HTTPException(
            status_code=400,
            detail="Доступен призыв только ×1 или ×10",
        )

    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()

    try:
        connection.execute("BEGIN IMMEDIATE")
        current = load_player(connection, telegram_id)

        if (
            int(current.get("level", 1))
            < COMPANION_SYSTEM_UNLOCK_LEVEL
        ):
            connection.commit()
            return build_player_response(
                current,
                companion_summoned=False,
                companion_summon_results=[],
                message=(
                    f"🔒 Система спутников откроется "
                    f"на {COMPANION_SYSTEM_UNLOCK_LEVEL} уровне"
                ),
            )

        cost = COMPANION_SUMMON_COST * count
        scrolls = max(
            0,
            int(current.get("companion_scrolls", 0)),
        )

        if scrolls < cost:
            connection.commit()
            return build_player_response(
                current,
                companion_summoned=False,
                companion_summon_results=[],
                companion_summon_cost=cost,
                missing_companion_scrolls=(
                    cost - scrolls
                ),
                message=(
                    f"🐾 Недостаточно свитков спутников: "
                    f"нужно {cost}, есть {scrolls}"
                ),
            )

        collection = normalize_companion_collection(
            current.get("companions_collection_json")
        )
        summon_exp = max(
            0,
            int(current.get("companion_summon_exp", 0)),
        )
        old_summon_level = (
            companion_summon_level_from_exp(
                summon_exp
            )
        )
        results = []

        for _ in range(count):
            current_summon_level = (
                companion_summon_level_from_exp(
                    summon_exp
                )
            )
            companion_id = roll_companion_id(
                current_summon_level
            )
            result = apply_companion_summon(
                collection,
                companion_id,
            )
            result["summon_level"] = (
                current_summon_level
            )
            results.append(result)
            summon_exp += 1

        new_summon_level = (
            companion_summon_level_from_exp(
                summon_exp
            )
        )
        remaining_scrolls = scrolls - cost
        now = int(time.time())

        connection.execute(
            """
            UPDATE players
            SET companion_scrolls = ?,
                companion_summon_exp = ?,
                companions_collection_json = ?,
                updated_at = ?
            WHERE telegram_id = ?
            """,
            (
                remaining_scrolls,
                summon_exp,
                json.dumps(
                    collection,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                now,
                telegram_id,
            ),
        )
        connection.commit()
        updated = load_player(
            connection,
            telegram_id,
        )
    finally:
        connection.close()

    new_count = sum(
        1
        for result in results
        if result["new"]
    )
    levels_gained = sum(
        int(result["levels_gained"])
        for result in results
    )
    summon_levels_gained = (
        new_summon_level - old_summon_level
    )

    message = f"🐾 Призвано спутников: {count}"

    if new_count > 0:
        message += f". Новых спутников: {new_count}"

    if levels_gained > 0:
        message += (
            f". Улучшений спутников: {levels_gained}"
        )

    if summon_levels_gained > 0:
        message += (
            f". Уровень призыва повышен "
            f"до {new_summon_level}"
        )

    return build_player_response(
        updated,
        companion_summoned=True,
        companion_summon_count=count,
        companion_summon_cost=cost,
        companion_summon_results=results,
        new_companions_count=new_count,
        companion_levels_gained=levels_gained,
        companion_summon_level_before=(
            old_summon_level
        ),
        companion_summon_level_after=(
            new_summon_level
        ),
        companion_summon_levels_gained=(
            summon_levels_gained
        ),
        message=message,
    )


@app.post("/skills/auto/enable")
def enable_skills_auto(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    connection.execute(
        "UPDATE players SET skills_auto_enabled = 1, updated_at = ? "
        "WHERE telegram_id = ?",
        (int(time.time()), telegram_id),
    )
    connection.commit()
    updated = load_player(connection, telegram_id)
    connection.close()
    return build_player_response(
        updated,
        skills_auto_enabled=True,
        message="Авто навыков включено",
    )


@app.post("/skills/auto/disable")
def disable_skills_auto(x_telegram_init_data: str = Header(...)) -> dict:
    user = validate_telegram_data(x_telegram_init_data)
    player_data = get_or_create_player(user)
    telegram_id = int(player_data["telegram_id"])
    connection = get_database()
    connection.execute(
        "UPDATE players SET skills_auto_enabled = 0, updated_at = ? "
        "WHERE telegram_id = ?",
        (int(time.time()), telegram_id),
    )
    connection.commit()
    updated = load_player(connection, telegram_id)
    connection.close()
    return build_player_response(
        updated,
        skills_auto_enabled=False,
        message="Авто навыков выключено",
    )


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
