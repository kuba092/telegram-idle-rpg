"""Central, deterministic formulas for skill/companion progression."""
from __future__ import annotations

import hashlib
from typing import Any, Mapping
from awakening_system import normalize_rank_state

MAX_PROGRESSION_LEVEL = 50
MILESTONE_LEVELS = (5, 10, 20, 30, 40, 50)
SKILL_EFFECT_BONUSES = {5: .02, 20: .03, 40: .05, 50: .05}
SKILL_COOLDOWN_BONUSES = {10: .02, 30: .03}
COMPANION_MILESTONE_MULTIPLIERS = {5: 1.02, 10: 1.03, 20: 1.05, 30: 1.07, 40: 1.10, 50: 1.15}

# Premium crystals are not a farming-loop reward. Limited future grants may be
# implemented only by a separate, auditable quest/reward transaction (daily or
# weekly quests, achievements, first clears, events, compensation or rankings).
# This package intentionally implements neither that transaction nor quest routes.
PREMIUM_CRYSTAL_REPEATABLE_SOURCES = frozenset({
    "normal_battle", "elite_battle", "boss_battle", "chest_open",
    "chest_upgrade", "salvage", "bulk_salvage", "reroll",
    "skill_upgrade", "companion_upgrade",
})
FUTURE_LIMITED_PREMIUM_CRYSTAL_SOURCES = frozenset({
    "daily_quest", "weekly_quest", "achievement", "first_stage_clear",
    "event", "compensation", "ranking_reward",
})


def skill_upgrade_cost(level: int) -> int:
    return round(2 + 1.35 * max(1, int(level)) ** 1.35)


def skill_gold_cost(level: int) -> int:
    return round(25 * 1.22 ** (max(1, int(level)) - 1))


def companion_upgrade_cost(level: int) -> int:
    return round(2 + 1.30 * max(1, int(level)) ** 1.38)


def companion_gold_cost(level: int) -> int:
    return round(20 * 1.20 ** (max(1, int(level)) - 1))


def active_milestones(level: int) -> list[int]:
    return [value for value in MILESTONE_LEVELS if int(level) >= value]


def next_milestone(level: int) -> int | None:
    return next((value for value in MILESTONE_LEVELS if int(level) < value), None)


def skill_effective_multiplier(level: int) -> float:
    return round(1 + sum(value for milestone, value in SKILL_EFFECT_BONUSES.items() if int(level) >= milestone), 4)


def skill_cooldown_multiplier(level: int) -> float:
    return round(1 - sum(value for milestone, value in SKILL_COOLDOWN_BONUSES.items() if int(level) >= milestone), 4)


def companion_milestone_multiplier(level: int) -> float:
    reached = active_milestones(level)
    return COMPANION_MILESTONE_MULTIPLIERS[reached[-1]] if reached else 1.0


def progression_entry(raw: Any, *, owned_default: bool = True) -> dict:
    entry = raw if isinstance(raw, Mapping) else {}
    def safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    level = min(MAX_PROGRESSION_LEVEL, max(1, safe_int(entry.get("level", 1), 1)))
    return {"owned": bool(entry.get("owned", owned_default)), "level": level,
            "experience": max(0, safe_int(entry.get("experience", 0), 0)),
            "upgrade_count": max(0, safe_int(entry.get("upgrade_count", level - 1), level - 1)),
            "max_level": MAX_PROGRESSION_LEVEL, **normalize_rank_state(entry)}


def skill_public(level: int, base_cooldown: float | None = None) -> dict:
    level = min(MAX_PROGRESSION_LEVEL, max(1, int(level)))
    result = {"level": level, "max_level": MAX_PROGRESSION_LEVEL,
              "upgrade_cost": {"skill_tomes": 0 if level >= 50 else skill_upgrade_cost(level),
                               "gold": 0 if level >= 50 else skill_gold_cost(level)},
              "milestone_progress": active_milestones(level), "active_milestones": active_milestones(level),
              "next_milestone": next_milestone(level), "mastery_unlocked": level >= 50,
              "effective_multiplier": skill_effective_multiplier(level)}
    result["effective_cooldown"] = (round(base_cooldown * skill_cooldown_multiplier(level), 3)
                                      if base_cooldown is not None else None)
    return result


def companion_public(level: int, preview: Any = None) -> dict:
    level = min(MAX_PROGRESSION_LEVEL, max(1, int(level)))
    return {"level": level, "max_level": MAX_PROGRESSION_LEVEL,
            "upgrade_cost": {"companion_essence": 0 if level >= 50 else companion_upgrade_cost(level),
                             "gold": 0 if level >= 50 else companion_gold_cost(level)},
            "active_milestones": active_milestones(level), "next_milestone": next_milestone(level),
            "mastery_unlocked": level >= 50, "milestone_multiplier": companion_milestone_multiplier(level),
            "effective_effect_preview": preview}


def victory_progression_reward(identity: Any, stage: int, *, boss: bool, elite: bool) -> dict:
    digest = hashlib.sha256(f"progression-reward-v1:{identity}".encode()).digest()
    details = {"seed_version": "progression-reward-v1", "roll": int.from_bytes(digest[:8], "big") / 2**64}
    if boss:
        amount = min(8, 2 + max(0, int(stage) - 1) // 100)
        # One exclusive deterministic 8% roll; never premium currency.
        summon_roll = digest[10] % 100
        return {"skill_tomes_gained": amount, "companion_essence_gained": amount,
                "skill_summon_scrolls_gained": int(summon_roll < 4),
                "companion_summon_contracts_gained": int(4 <= summon_roll < 8),
                "source": "boss", "roll_details": details}
    if elite:
        amount = 1 + digest[8] % 2
        tomes = amount if digest[9] % 2 == 0 else 0
        return {"skill_tomes_gained": tomes, "companion_essence_gained": amount - tomes,
                "source": "elite", "roll_details": details}
    # One exclusive 12% roll: 6% tomes, 6% essence.
    roll = details["roll"]
    return {"skill_tomes_gained": int(roll < .06), "companion_essence_gained": int(.06 <= roll < .12),
            "source": "normal", "roll_details": details}
