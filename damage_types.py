"""Shared damage types, resistance math and deterministic enemy profiles."""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any, Mapping

DAMAGE_TYPES = ("physical", "nature", "poison", "arcane", "true")
RESISTED_DAMAGE_TYPES = DAMAGE_TYPES[:-1]
ENEMY_ARCHETYPES = ("brute", "mystic", "toxic", "guardian")
ENEMY_ATTACK_TYPES = {
    "brute": "physical", "mystic": "arcane",
    "toxic": "poison", "guardian": "nature",
}

ARCHETYPE_RESISTANCES = {
    "brute": {"physical": .24, "nature": .04, "poison": .02, "arcane": -.12},
    "mystic": {"physical": -.12, "nature": .03, "poison": .02, "arcane": .24},
    "toxic": {"physical": .03, "nature": -.12, "poison": .24, "arcane": .02},
    "guardian": {"physical": .12, "nature": .12, "poison": .12, "arcane": .12},
}


def clamp_resistance(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(-.50, min(.75, number))


def clamp_penetration(value: Any) -> float:
    """Normalize penetration represented as a fraction (0.50 == 50 p.p.)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return max(0.0, min(.50, number))


def normalize_resistances(value: Any) -> dict[str, float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
    source = value if isinstance(value, Mapping) else {}
    return {kind: round(clamp_resistance(source.get(kind, 0)), 4)
            for kind in RESISTED_DAMAGE_TYPES}


def fortified_resistances(value: Any) -> dict[str, float]:
    """Add 12 p.p. while retaining at least one weakness at or below 20%."""
    resistances = normalize_resistances(value)
    weakest = min(resistances, key=lambda kind: (resistances[kind], kind))
    fortified = {kind: round(clamp_resistance(amount + .12), 4)
                 for kind, amount in resistances.items()}
    fortified[weakest] = min(.20, fortified[weakest])
    return fortified


def resistance_breakdown(damage: Any, damage_type: str,
                         resistance: Any = 0, penetration: Any = 0,
                         temporary_modifier: Any = 0) -> dict[str, Any]:
    before_damage = max(0, round(float(damage or 0)))
    kind = damage_type if damage_type in DAMAGE_TYPES else "physical"
    if kind == "true":
        return {
            "damage_type": kind, "base_resistance": 0.0,
            "temporary_resistance_modifier": 0.0, "resistance_before": 0.0,
            "resistance_after_penetration": 0.0, "penetration_used": 0.0,
            "damage_before_resistance": before_damage,
            "damage_after_resistance": before_damage, "resistance_reduction": 0,
        }
    base = clamp_resistance(resistance)
    try:
        temporary = float(temporary_modifier)
    except (TypeError, ValueError):
        temporary = 0.0
    # Temporary modifiers are applied to the base, then penetration and the
    # existing resistance clamp.  This preserves the established formula.
    before_penetration = clamp_resistance(base + temporary)
    used = clamp_penetration(penetration)
    effective = clamp_resistance(before_penetration - used)
    after_damage = max(0, round(before_damage * (1 - effective)))
    return {
        "damage_type": kind, "base_resistance": base,
        "temporary_resistance_modifier": temporary,
        "resistance_before": before_penetration,
        "resistance_after_penetration": effective, "penetration_used": used,
        "damage_before_resistance": before_damage,
        "damage_after_resistance": after_damage,
        "resistance_reduction": before_damage - after_damage,
    }


def incoming_damage_breakdown(damage: Any, damage_type: str,
                              hero_resistances: Mapping[str, Any] | None,
                              reduction_multiplier: Any = 1.0) -> dict[str, Any]:
    """Server-side incoming order after dodge: resistance, then gear reduction."""
    resistances = normalize_resistances(hero_resistances)
    resistance = resistance_breakdown(
        damage, damage_type, resistances.get(damage_type, 0), 0,
    )
    try:
        multiplier = max(0.0, float(reduction_multiplier))
    except (TypeError, ValueError):
        multiplier = 1.0
    after_reduction = max(0, round(resistance["damage_after_resistance"] * multiplier))
    return {
        **resistance,
        "damage_after_reduction": after_reduction,
        "equipment_reduction": resistance["damage_after_resistance"] - after_reduction,
    }


def generate_enemy_profile(stage: int, boss: bool = False, seed: Any = None) -> dict[str, Any]:
    """Generate a stable profile without touching HP or economy tuning."""
    stable_seed = f"enemy-resistance-v1:{int(stage)}:{int(bool(boss))}:{seed!s}"
    rng = random.Random(int.from_bytes(hashlib.sha256(stable_seed.encode()).digest()[:8], "big"))
    archetype = ENEMY_ARCHETYPES[rng.randrange(len(ENEMY_ARCHETYPES))]
    progress = min(.08, max(0, int(stage) - 1) / 10000)
    boss_bonus = .08 if boss else 0.0
    resistances = {}
    for kind, base in ARCHETYPE_RESISTANCES[archetype].items():
        # Small deterministic texture, while preserving each archetype's weakness.
        value = base + progress + boss_bonus + rng.uniform(-.025, .025)
        resistances[kind] = round(clamp_resistance(value), 4)
    weak_kind = min(resistances, key=resistances.get)
    # Bosses must retain a meaningful weakness even at high stages.
    if boss and resistances[weak_kind] > -.02:
        resistances[weak_kind] = -.02
    strong_kind = max(resistances, key=resistances.get)
    return {
        "enemy_archetype": archetype,
        "enemy_attack_type": ENEMY_ATTACK_TYPES[archetype],
        "resistances": resistances,
        "weakness": {"damage_type": weak_kind, "resistance": resistances[weak_kind]},
        "strongest_resistance": {"damage_type": strong_kind, "resistance": resistances[strong_kind]},
    }


def enemy_profile(player: Mapping[str, Any], *, generate_missing: bool = False) -> dict[str, Any]:
    stored = normalize_resistances(player.get("enemy_resistances_json"))
    archetype = str(player.get("enemy_archetype") or "")
    has_stored = bool(player.get("enemy_resistances_json")) and archetype in ENEMY_ARCHETYPES
    if not has_stored and generate_missing:
        return generate_enemy_profile(
            int(player.get("stage", 1)), bool(int(player.get("boss_active", 0))),
            f"{player.get('telegram_id', 0)}:{player.get('kills_in_stage', 0)}:{player.get('enemy_max_hp', 0)}",
        )
    if archetype not in ENEMY_ARCHETYPES:
        archetype = "guardian"
    weak = min(stored, key=stored.get)
    strong = max(stored, key=stored.get)
    return {
        "enemy_archetype": archetype,
        "enemy_attack_type": ENEMY_ATTACK_TYPES[archetype],
        "resistances": stored,
        "weakness": {"damage_type": weak, "resistance": stored[weak]},
        "strongest_resistance": {"damage_type": strong, "resistance": stored[strong]},
    }
