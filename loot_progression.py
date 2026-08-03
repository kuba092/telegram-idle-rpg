"""Central formulas and normalization for the equipment loot loop.

This module deliberately has no database or FastAPI dependencies.  Routes keep
transaction ownership while tests and simulations can use the same formulas.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any, Iterable, Mapping

RARITY_ORDER = (
    "common", "uncommon", "rare", "epic", "legendary", "mythic",
    "ancient", "divine", "celestial",
)
RARITY_INDEX = {name: index for index, name in enumerate(RARITY_ORDER)}
BASE_DUST = dict(zip(RARITY_ORDER, (1, 2, 4, 8, 16, 28, 45, 70, 110)))
SALVAGE_CHEST_XP = dict(zip(RARITY_ORDER, (1, 1, 2, 2, 3, 3, 4, 4, 5)))
BASE_REROLL_COST = {
    "uncommon": 8, "rare": 12, "epic": 20, "legendary": 35,
    "mythic": 60, "ancient": 100, "divine": 160, "celestial": 250,
}
REROLL_CRYSTAL_COST = {
    "uncommon": 0, "rare": 0, "epic": 0, "legendary": 1,
    "mythic": 1, "ancient": 2, "divine": 3, "celestial": 5,
}
CHEST_LEVEL_CAP = 30
INVENTORY_BASE_CAPACITY = 50
INVENTORY_CAP = 100

# Rows correspond to levels 1-5, 6-10, ... 26-30.  Keeping this table here
# makes balance changes reviewable and keeps routes free of progression math.
CHEST_RARITY_WEIGHTS = (
    (76, 21.5, 2.4, .1, 0, 0, 0, 0, 0),
    (55, 31, 11.5, 2.3, .2, 0, 0, 0, 0),
    (36, 31, 22, 9, 1.8, .2, 0, 0, 0),
    (24, 27, 27, 16, 5, .9, .1, 0, 0),
    (16, 22, 28, 22, 9, 2.7, .28, .02, 0),
    (11, 18, 27, 25, 12, 5.5, 1.25, .23, .02),
)


def normalize_rarity(value: Any) -> str:
    value = str(value or "common").lower()
    return value if value in RARITY_INDEX else "common"


def normalize_item_progression(item: Any) -> dict:
    if not isinstance(item, Mapping):
        return {}
    result = dict(item)
    item_id = result.get("item_id", result.get("id", ""))
    result["id"] = str(item_id)
    result["item_id"] = str(item_id)
    result["locked"] = bool(result.get("locked", False))
    result["reroll_count"] = max(0, int(result.get("reroll_count", 0) or 0))
    result["item_version"] = max(1, int(result.get("item_version", 1) or 1))
    history = result.get("reroll_history", [])
    result["reroll_history"] = list(history[-5:]) if isinstance(history, list) else []
    return result


def chest_xp_required(level: int) -> int:
    return round(20 * (1.18 ** (max(1, int(level)) - 1)))


def chest_progress(player: Mapping[str, Any]) -> dict:
    level = min(CHEST_LEVEL_CAP, max(1, int(player.get("chest_level", 1) or 1)))
    xp = max(0, int(player.get("chest_xp", 0) or 0))
    required = chest_xp_required(level) if level < CHEST_LEVEL_CAP else 0
    return {
        "salvage_dust": max(0, int(player.get("salvage_dust", 0) or 0)),
        "refinement_crystal": max(0, int(player.get("refinement_crystal", 0) or 0)),
        "chest_xp": xp, "chest_level": level, "chest_xp_current": xp,
        "chest_xp_required": required,
        "chest_upgrade_ready": level < CHEST_LEVEL_CAP and xp >= required,
        "next_chest_level": min(CHEST_LEVEL_CAP, level + 1),
    }


def inventory_capacity(chest_level: int) -> int:
    # The first bonus is reached at chest level 5.
    return min(INVENTORY_CAP, INVENTORY_BASE_CAPACITY + 5 * (max(1, int(chest_level)) // 5))


def rarity_weights(chest_level: int) -> tuple[float, ...]:
    level = min(CHEST_LEVEL_CAP, max(1, int(chest_level)))
    band = min(len(CHEST_RARITY_WEIGHTS) - 1, (level - 1) // 5)
    return CHEST_RARITY_WEIGHTS[band]


def deterministic_rng(*parts: Any) -> random.Random:
    digest = hashlib.sha256(":".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))


def salvage_dust_value(rarity: Any, chest_level: int, effective_stage: int) -> int:
    rarity = normalize_rarity(rarity)
    stage_scale = 1 + min(max(0, int(effective_stage)), 1000) * .001
    value = BASE_DUST[rarity] * (1 + .08 * (max(1, int(chest_level)) - 1)) * stage_scale
    return max(1, round(value))


def crystal_reward(rarity: Any, rng: random.Random) -> int:
    rarity = normalize_rarity(rarity)
    if rarity in {"common", "uncommon", "rare"}: return 0
    if rarity == "epic": return int(rng.random() < .05)
    if rarity == "legendary": return int(rng.random() < .12)
    if rarity == "mythic": return 1
    low, high = {"ancient": (1, 2), "divine": (2, 3), "celestial": (3, 5)}[rarity]
    return rng.randint(low, high)


def salvage_reward(item: Mapping[str, Any], chest_level: int, effective_stage: int,
                   rng: random.Random | None = None) -> dict:
    normalized = normalize_item_progression(item)
    rarity = normalize_rarity(normalized.get("rarity"))
    rng = rng or deterministic_rng("salvage-v1", normalized.get("item_id"), rarity,
                                   chest_level, effective_stage)
    return {"rarity": rarity,
            "dust": salvage_dust_value(rarity, chest_level, effective_stage),
            "crystals": crystal_reward(rarity, rng),
            "chest_xp": SALVAGE_CHEST_XP[rarity]}


def reroll_cost(rarity: Any, reroll_count: int) -> dict:
    rarity = normalize_rarity(rarity)
    base = BASE_REROLL_COST.get(rarity)
    if base is None:
        return {"dust": 0, "crystals": 0, "available": False}
    return {"dust": round(base * (1 + max(0, int(reroll_count)) * .35)),
            "crystals": REROLL_CRYSTAL_COST[rarity], "available": True}


def rarity_at_or_below(rarity: Any, maximum: Any) -> bool:
    rarity, maximum = normalize_rarity(rarity), str(maximum or "off").lower()
    return maximum in RARITY_INDEX and RARITY_INDEX[rarity] <= RARITY_INDEX[maximum]


def unique_ids(values: Iterable[Any], limit: int = 100) -> list[str]:
    result = []
    seen = set()
    for value in values:
        value = str(value)
        if value not in seen:
            seen.add(value); result.append(value)
        if len(result) >= limit: break
    return result
