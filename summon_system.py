"""Permanent summon banners: public config, deterministic pity and pull helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

RARITIES = ("common", "uncommon", "rare", "epic", "legendary")
RATES = {"common": 50.0, "uncommon": 30.0, "rare": 14.0, "epic": 5.0, "legendary": 1.0}
RATE_WEIGHTS = {key: round(value * 100) for key, value in RATES.items()}
FRAGMENTS = {"common": 1, "uncommon": 2, "rare": 5, "epic": 12, "legendary": 30}
FALLBACK = {"legendary": ("legendary", "epic", "rare", "uncommon", "common"),
            "epic": ("epic", "rare", "uncommon", "common"),
            "rare": ("rare", "uncommon", "common"), "uncommon": ("uncommon", "common"),
            "common": ("common",)}
BANNERS = {
    "skill_standard": {"entity_type": "skill", "ticket_field": "skill_summon_scrolls"},
    "companion_standard": {"entity_type": "companion", "ticket_field": "companion_summon_contracts"},
}
COSTS = {1: {"ticket": 1, "premium_crystals": 100}, 10: {"ticket": 10, "premium_crystals": 900}}


def normalize_state(value: Any) -> dict:
    try: raw = value if isinstance(value, Mapping) else json.loads(value or "{}")
    except (TypeError, ValueError): raw = {}
    if not isinstance(raw, Mapping): raw = {}
    def n(key):
        try: return max(0, int(raw.get(key, 0)))
        except (TypeError, ValueError): return 0
    history = raw.get("summon_history", [])
    return {"total_summons": n("total_summons"), "pulls_since_rare": n("pulls_since_rare"),
            "pulls_since_epic": n("pulls_since_epic"), "pulls_since_legendary": n("pulls_since_legendary"),
            "pity_version": max(1, n("pity_version")),
            "summon_history": list(history)[-100:] if isinstance(history, list) else []}


def guarantee(state: Mapping[str, Any]) -> str | None:
    if int(state["pulls_since_legendary"]) + 1 >= 150: return "legendary"
    if int(state["pulls_since_epic"]) + 1 >= 50: return "epic"
    if int(state["pulls_since_rare"]) + 1 >= 10: return "rare"
    return None


def _random_u64(seed: str, pull_index: int, purpose: str) -> int:
    return int.from_bytes(hashlib.sha256(f"summon-v1:{seed}:{pull_index}:{purpose}".encode()).digest()[:8], "big")


def roll_rarity(seed: str, pull_index: int, minimum: str | None = None) -> str:
    roll = _random_u64(seed, pull_index, "rarity") % 10000
    cursor, result = 0, "legendary"
    for rarity in RARITIES:
        cursor += RATE_WEIGHTS[rarity]
        if roll < cursor: result = rarity; break
    if minimum and RARITIES.index(result) < RARITIES.index(minimum): return minimum
    return result


def choose_entity(catalog: Mapping[str, Mapping[str, Any]], rarity: str, seed: str, pull_index: int) -> tuple[str, str]:
    for actual in FALLBACK[rarity]:
        choices = sorted(key for key, item in catalog.items() if str(item.get("rarity", "common")) == actual)
        if choices:
            return choices[_random_u64(seed, pull_index, "entity") % len(choices)], actual
    raise ValueError("banner catalog is empty")


def advance(state: dict, rarity: str) -> None:
    state["total_summons"] += 1
    state["pulls_since_rare"] = 0 if RARITIES.index(rarity) >= 2 else state["pulls_since_rare"] + 1
    state["pulls_since_epic"] = 0 if RARITIES.index(rarity) >= 3 else state["pulls_since_epic"] + 1
    state["pulls_since_legendary"] = 0 if rarity == "legendary" else state["pulls_since_legendary"] + 1


def public_banner(banner_id: str, state: Mapping[str, Any], player: Mapping[str, Any]) -> dict:
    config = BANNERS[banner_id]
    return {"banner_id": banner_id, "entity_type": config["entity_type"], "rates": dict(RATES),
            "rarity_fallback": {key: list(value) for key, value in FALLBACK.items()},
            "costs": {"x1": COSTS[1], "x10": COSTS[10]}, "total_summons": int(state["total_summons"]),
            "pulls_until_rare_guarantee": max(0, 10-int(state["pulls_since_rare"])),
            "pulls_until_epic_guarantee": max(0, 50-int(state["pulls_since_epic"])),
            "pulls_until_legendary_guarantee": max(0, 150-int(state["pulls_since_legendary"])),
            "pity_version": int(state["pity_version"]),
            "available_tickets": max(0, int(player.get(config["ticket_field"], 0))),
            "premium_crystals": max(0, int(player.get("premium_crystals", 0)))}


def normalize_fragments(value: Any, valid_ids: set[str]) -> dict[str, int]:
    try: raw = value if isinstance(value, Mapping) else json.loads(value or "{}")
    except (TypeError, ValueError): raw = {}
    if not isinstance(raw, Mapping): return {}
    result = {}
    for key, value in raw.items():
        try: amount = max(0, int(value))
        except (TypeError, ValueError): amount = 0
        if key in valid_ids and amount: result[str(key)] = amount
    return result
