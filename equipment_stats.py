"""Secondary equipment stats, generation, caps and build comparison."""

from __future__ import annotations

import random
from typing import Any, Mapping


SECONDARY_STATS = {
    "attack_speed": {"name": "Скорость атаки", "format": "+{value:.1f}%", "min": 0.0, "max": 50.0, "combine": "add"},
    "crit_chance": {"name": "Шанс крита", "format": "+{value:.1f} п.п.", "min": 0.0, "max": 100.0, "combine": "add"},
    "crit_damage": {"name": "Критический урон", "format": "+{value:.1f} п.п.", "min": 0.0, "max": 200.0, "combine": "add"},
    "skill_damage": {"name": "Урон навыков", "format": "+{value:.1f}%", "min": 0.0, "max": 150.0, "combine": "add"},
    "companion_damage": {"name": "Урон спутников", "format": "+{value:.1f}%", "min": 0.0, "max": 150.0, "combine": "add"},
    "boss_damage": {"name": "Урон боссам", "format": "+{value:.1f}%", "min": 0.0, "max": 100.0, "combine": "add"},
    "incoming_damage_reduction": {"name": "Снижение входящего урона", "format": "-{value:.1f}%", "min": 0.0, "max": 60.0, "combine": "add"},
    "healing_bonus": {"name": "Эффективность лечения", "format": "+{value:.1f}%", "min": 0.0, "max": 100.0, "combine": "add"},
    "dodge_chance": {"name": "Уклонение", "format": "+{value:.1f}%", "min": 0.0, "max": 50.0, "combine": "add"},
    "combo_chance": {"name": "Шанс комбо", "format": "+{value:.1f}%", "min": 0.0, "max": 50.0, "combine": "add"},
    "counter_chance": {"name": "Шанс контратаки", "format": "+{value:.1f}%", "min": 0.0, "max": 50.0, "combine": "add"},
    "physical_penetration": {"name": "Пробивание физической защиты", "format": "+{value:.1f} п.п.", "min": 0.0, "max": 50.0, "combine": "add"},
    "nature_penetration": {"name": "Пробивание защиты природы", "format": "+{value:.1f} п.п.", "min": 0.0, "max": 50.0, "combine": "add"},
    "poison_penetration": {"name": "Пробивание защиты от яда", "format": "+{value:.1f} п.п.", "min": 0.0, "max": 50.0, "combine": "add"},
    "arcane_penetration": {"name": "Пробивание магической защиты", "format": "+{value:.1f} п.п.", "min": 0.0, "max": 50.0, "combine": "add"},
}

PENETRATION_STATS = {key for key in SECONDARY_STATS if key.endswith("_penetration")}

STAT_COUNT = {
    "common": (0, 0), "uncommon": (0, 1), "rare": (1, 1),
    "epic": (1, 2), "legendary": (2, 2), "mythic": (2, 3),
    "ancient": (3, 3), "divine": (3, 4), "celestial": (4, 4),
}
RARITY_SCALE = {
    "common": .45, "uncommon": .60, "rare": .75, "epic": .95,
    "legendary": 1.15, "mythic": 1.35, "ancient": 1.55,
    "divine": 1.80, "celestial": 2.05,
}

ATTACKING = {"attack_speed", "crit_chance", "crit_damage", "skill_damage", "boss_damage", "combo_chance", *PENETRATION_STATS}
DEFENSIVE = {"incoming_damage_reduction", "healing_bonus", "dodge_chance", "counter_chance"}
SLOT_STAT_WEIGHTS = {
    "attack": {key: (5.0 if key in ATTACKING else 1.0) for key in SECONDARY_STATS},
    "defense": {key: (5.0 if key in DEFENSIVE else 1.0) for key in SECONDARY_STATS},
    "mixed": {key: 1.0 for key in SECONDARY_STATS},
}

# Base roll is percentage points. Progress raises magnitude slowly, while rarity
# is the dominant factor; the final aggregate is always capped separately.
BASE_ROLL = {
    "attack_speed": 2.5, "crit_chance": 2.0, "crit_damage": 7.0,
    "skill_damage": 3.5, "companion_damage": 3.5, "boss_damage": 3.0,
    "incoming_damage_reduction": 2.0, "healing_bonus": 3.5,
    "dodge_chance": 1.6, "combo_chance": 1.6, "counter_chance": 1.6,
    "physical_penetration": 2.5, "nature_penetration": 2.5,
    "poison_penetration": 2.5, "arcane_penetration": 2.5,
}

BUILD_WEIGHTS = {
    "damage": {"power": .08, "damage": 1.0, "hp": .01, "attack_speed": 3.0, "crit_chance": 2.4, "crit_damage": .65, "skill_damage": 1.8, "companion_damage": 1.0, "boss_damage": 1.5, "combo_chance": 2.2, "incoming_damage_reduction": .2, "healing_bonus": .1, "dodge_chance": .2, "counter_chance": .2},
    "defense": {"power": .05, "damage": .1, "hp": .10, "attack_speed": .1, "crit_chance": .1, "crit_damage": .03, "skill_damage": .1, "companion_damage": .1, "boss_damage": .1, "combo_chance": .2, "incoming_damage_reduction": 4.0, "healing_bonus": 1.7, "dodge_chance": 3.2, "counter_chance": 1.6},
    "balanced": {"power": .08, "damage": .65, "hp": .055, "attack_speed": 1.5, "crit_chance": 1.2, "crit_damage": .32, "skill_damage": .9, "companion_damage": .6, "boss_damage": .7, "combo_chance": 1.1, "incoming_damage_reduction": 2.0, "healing_bonus": .85, "dodge_chance": 1.6, "counter_chance": .9},
}
for _profile, _weight in (("damage", 1.4), ("balanced", .65), ("defense", .05)):
    BUILD_WEIGHTS[_profile].update({key: _weight for key in PENETRATION_STATS})


def normalize_secondary_stats(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, raw in value.items():
        definition = SECONDARY_STATS.get(str(key))
        if definition is None:
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        result[str(key)] = round(max(definition["min"], min(definition["max"], number)), 4)
    return result


def normalize_item(item: Any) -> dict:
    if not isinstance(item, Mapping):
        return {}
    result = dict(item)
    result["secondary_stats"] = normalize_secondary_stats(item.get("secondary_stats"))
    result["secondary_stats_public"] = public_secondary_stats(result["secondary_stats"])
    return result


def aggregate_secondary_stats(equipment: Mapping[str, Any]) -> dict[str, float]:
    totals = {key: 0.0 for key in SECONDARY_STATS}
    for item in equipment.values():
        for key, value in normalize_secondary_stats(item.get("secondary_stats") if isinstance(item, Mapping) else {}).items():
            totals[key] += value
    return {key: round(min(value, SECONDARY_STATS[key]["max"]), 4) for key, value in totals.items()}


def public_secondary_stats(stats: Mapping[str, Any]) -> list[dict]:
    normalized = normalize_secondary_stats(stats)
    return [{"key": key, "name": SECONDARY_STATS[key]["name"], "value": value,
             "formatted": SECONDARY_STATS[key]["format"].format(value=value),
             "min": SECONDARY_STATS[key]["min"], "max": SECONDARY_STATS[key]["max"],
             "combine": SECONDARY_STATS[key]["combine"]} for key, value in normalized.items()]


def generate_secondary_stats(rarity: str, chest_level: int, effective_stage: int,
                             slot_focus: str, rng: random.Random | Any = random) -> dict[str, float]:
    low, high = STAT_COUNT.get(rarity, (0, 0))
    count = rng.randint(low, high)
    if count == 0:
        return {}
    available = list(SECONDARY_STATS)
    rarity_order = list(STAT_COUNT)
    if rarity not in rarity_order or rarity_order.index(rarity) < rarity_order.index("rare"):
        available = [key for key in available if key not in PENETRATION_STATS]
    selected = []
    weights = SLOT_STAT_WEIGHTS.get(slot_focus, SLOT_STAT_WEIGHTS["mixed"])
    for _ in range(min(count, len(available))):
        key = rng.choices(available, weights=[weights[name] for name in available], k=1)[0]
        selected.append(key)
        available.remove(key)
    progress = 1.0 + min(1.5, max(0, effective_stage - 1) / 800 + max(0, chest_level - 1) / 48)
    scale = RARITY_SCALE.get(rarity, 1.0) * progress
    return {key: round(min(SECONDARY_STATS[key]["max"], BASE_ROLL[key] * scale * rng.uniform(.85, 1.15)), 2) for key in selected}


def build_score(item: Any, profile: str = "balanced") -> float:
    item = normalize_item(item)
    weights = BUILD_WEIGHTS.get(profile, BUILD_WEIGHTS["balanced"])
    score = sum(max(0.0, float(item.get(key, 0) or 0)) * weights[key] for key in ("power", "damage", "hp"))
    score += sum(value * weights.get(key, 0.0) for key, value in item["secondary_stats"].items())
    return round(score, 2)


def comparison_breakdown(candidate: Any, equipped: Any, profile: str) -> list[dict]:
    candidate, equipped = normalize_item(candidate), normalize_item(equipped)
    rows = []
    for key in ("power", "damage", "hp", *SECONDARY_STATS):
        old = float(equipped.get(key, 0) if key in ("power", "damage", "hp") else equipped["secondary_stats"].get(key, 0))
        new = float(candidate.get(key, 0) if key in ("power", "damage", "hp") else candidate["secondary_stats"].get(key, 0))
        if old != new:
            name = SECONDARY_STATS[key]["name"] if key in SECONDARY_STATS else {"power": "БМ", "damage": "Урон", "hp": "HP"}[key]
            rows.append({"stat": key, "name": name, "old": old, "new": new, "delta": round(new-old, 4), "score_delta": round((new-old) * BUILD_WEIGHTS.get(profile, BUILD_WEIGHTS["balanced"]).get(key, 0), 2)})
    return rows
