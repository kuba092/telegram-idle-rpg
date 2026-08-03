"""Rank and linear awakening rules shared by API, previews and combat."""
from __future__ import annotations

from typing import Any, Mapping

MAX_RANK = 5
STARS_PER_RANK = 5
MAX_AWAKENING_TIER = 3
RANK_VERSION = 1
STAR_BASE_COST = {"common": 5, "uncommon": 8, "rare": 12, "epic": 20, "legendary": 35}
AWAKENING_BASE_COST = {1: 100, 2: 180, 3: 300}
AWAKENING_RARITY_MULTIPLIER = {"epic": 1.10, "legendary": 1.25}
NODE_IDS = {
    "skill": ("awakened_core", "awakened_flow", "awakened_mastery"),
    "companion": ("bonded_core", "bonded_flow", "bonded_mastery"),
}


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_rank_state(raw: Any) -> dict:
    entry = raw if isinstance(raw, Mapping) else {}
    rank = min(MAX_RANK, max(0, _integer(entry.get("rank"))))
    stars = min(STARS_PER_RANK - 1, max(0, _integer(entry.get("rank_stars"))))
    if rank >= MAX_RANK:
        stars = 0
    tier = min(MAX_AWAKENING_TIER, max(0, _integer(entry.get("awakening_tier"))))
    if rank < MAX_RANK:
        tier = 0
    known_nodes = set(NODE_IDS["skill"] + NODE_IDS["companion"])
    raw_nodes = entry.get("awakening_nodes", [])
    nodes = [str(node) for node in raw_nodes if str(node) in known_nodes] if isinstance(raw_nodes, list) else []
    return {
        "rank": rank,
        "rank_stars": stars,
        "awakening_tier": tier,
        "awakening_nodes": nodes[:tier],
        "rank_version": max(RANK_VERSION, _integer(entry.get("rank_version"), RANK_VERSION)),
    }


def total_stars(state: Mapping[str, Any]) -> int:
    normalized = normalize_rank_state(state)
    return normalized["rank"] * STARS_PER_RANK + normalized["rank_stars"]


def star_cost(rarity: str, state_or_total: Mapping[str, Any] | int) -> int | None:
    stars = total_stars(state_or_total) if isinstance(state_or_total, Mapping) else max(0, int(state_or_total))
    if stars >= MAX_RANK * STARS_PER_RANK:
        return None
    return round(STAR_BASE_COST.get(str(rarity).lower(), STAR_BASE_COST["common"]) * (1 + stars * .22))


def awakening_cost(rarity: str, current_tier: int) -> int | None:
    next_tier = max(0, int(current_tier)) + 1
    base = AWAKENING_BASE_COST.get(next_tier)
    return None if base is None else round(base * AWAKENING_RARITY_MULTIPLIER.get(str(rarity).lower(), 1.0))


def advance_star(state: Mapping[str, Any]) -> tuple[dict, bool]:
    result = normalize_rank_state(state)
    if result["rank"] >= MAX_RANK:
        raise ValueError("maximum rank reached")
    result["rank_stars"] += 1
    advanced = result["rank_stars"] == STARS_PER_RANK
    if advanced:
        result["rank"] += 1
        result["rank_stars"] = 0
    result["rank_version"] += 1
    return result, advanced


def advance_awakening(state: Mapping[str, Any]) -> dict:
    result = normalize_rank_state(state)
    if result["rank"] < MAX_RANK:
        raise ValueError("rank 5 required")
    if result["awakening_tier"] >= MAX_AWAKENING_TIER:
        raise ValueError("maximum awakening reached")
    result["awakening_tier"] += 1
    result["rank_version"] += 1
    return result


def rank_multiplier(state: Mapping[str, Any], entity_type: str = "skill") -> float:
    normalized = normalize_rank_state(state)
    value = 1 + total_stars(normalized) * .012 + normalized["rank"] * .02
    tier = normalized["awakening_tier"]
    if entity_type == "skill":
        value += .05 if tier >= 1 else 0
    else:
        value += .05 * min(2, tier)
    return round(value, 6)


def mastery_multiplier(state: Mapping[str, Any], entity_type: str, role: str) -> float:
    if normalize_rank_state(state)["awakening_tier"] < 3:
        return 1.0
    if entity_type == "skill":
        return 1.05 if role == "control" else 1.08
    return 1.05 if role in ("defensive", "utility") else 1.10


def cooldown_multiplier(state: Mapping[str, Any]) -> float:
    return .97 if normalize_rank_state(state)["awakening_tier"] >= 2 else 1.0


def awakening_nodes(entity_type: str, tier: int) -> list[dict]:
    skill = entity_type == "skill"
    titles = (("Пробуждённое ядро", "Пробуждённый поток", "Пробуждённое мастерство") if skill
              else ("Ядро связи", "Поток связи", "Мастерство связи"))
    effects = (("+5% к эффективности", "-3% к перезарядке", "Уникальный модификатор навыка") if skill
               else ("+5% к эффекту", "Ещё +5% к эффекту", "Уникальный модификатор спутника"))
    return [{"node_id": node_id, "unlocked": index <= tier, "title": titles[index - 1],
             "description": effects[index - 1], "effect_summary": effects[index - 1]}
            for index, node_id in enumerate(NODE_IDS[entity_type], 1)]


def public_rank(entry: Mapping[str, Any], fragments: int, rarity: str, entity_type: str,
                effective_preview: Any = None) -> dict:
    state = normalize_rank_state(entry)
    balance = max(0, _integer(fragments))
    next_star = star_cost(rarity, state)
    next_awaken = awakening_cost(rarity, state["awakening_tier"]) if state["rank"] >= MAX_RANK else None
    nodes = awakening_nodes(entity_type, state["awakening_tier"])
    return {**state, "awakening_nodes": nodes, "total_stars": total_stars(state), "fragments": balance,
            "fragments_required": next_star, "next_fragment_cost": next_star,
            "next_awakening_cost": next_awaken, "rank_up_available": next_star is not None and balance >= next_star,
            "awakening_available": next_awaken is not None and balance >= next_awaken,
            "max_rank_reached": state["rank"] >= MAX_RANK,
            "max_awakening_reached": state["awakening_tier"] >= MAX_AWAKENING_TIER,
            "rank_multiplier": rank_multiplier(state, entity_type), "effective_preview": effective_preview}
