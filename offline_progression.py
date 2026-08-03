"""Pure, server-authoritative offline timing and aggregate reward formulas."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

OFFLINE_GRACE_SECONDS = 5 * 60
OFFLINE_CAP_SECONDS = 8 * 60 * 60
REWARD_UNIT_SECONDS = 60
OFFLINE_CLAIM_VERSION = 1


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_snapshot(value: Any) -> dict:
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        try:
            raw = json.loads(value or "{}")
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict) or _integer(raw.get("rewarded_seconds")) <= 0:
        return {}
    return {
        "version": OFFLINE_CLAIM_VERSION,
        "started_at": max(0, _integer(raw.get("started_at"))),
        "ended_at": max(0, _integer(raw.get("ended_at"))),
        "rewarded_seconds": min(OFFLINE_CAP_SECONDS, max(0, _integer(raw.get("rewarded_seconds")))),
        "stage": max(1, _integer(raw.get("stage"), 1)),
        "chest_level": max(1, _integer(raw.get("chest_level"), 1)),
    }


def elapsed(last_active_at: Any, server_time: Any) -> dict:
    total = max(0, _integer(server_time) - max(0, _integer(last_active_at)))
    rewarded = min(total, OFFLINE_CAP_SECONDS) if total >= OFFLINE_GRACE_SECONDS else 0
    return {"offline_seconds_total": total, "offline_seconds_rewarded": rewarded,
            "offline_cap_seconds": OFFLINE_CAP_SECONDS, "cap_reached": total >= OFFLINE_CAP_SECONDS}


def make_snapshot(player: Mapping[str, Any], server_time: int) -> dict:
    timing = elapsed(player.get("last_active_at"), server_time)
    if timing["offline_seconds_rewarded"] <= 0:
        return {}
    return {"version": OFFLINE_CLAIM_VERSION, "started_at": max(0, _integer(player.get("last_active_at"))),
            "ended_at": int(server_time), "rewarded_seconds": timing["offline_seconds_rewarded"],
            "stage": max(1, _integer(player.get("stage"), 1)),
            "chest_level": max(1, _integer(player.get("chest_level"), 1))}


def rewards(stage: Any, chest_level: Any, rewarded_seconds: Any, *, seed: str = "") -> dict:
    """Aggregate formula: O(1), with deterministic fractional distributions."""
    units = min(480, max(0, _integer(rewarded_seconds)) // REWARD_UNIT_SECONDS)
    stage_i, chest_i = max(1, _integer(stage, 1)), max(1, _integer(chest_level, 1))
    # Chest level participates in deterministic phase, without altering documented base rates.
    digest = hashlib.sha256(f"offline-v1:{seed}:{stage_i}:{chest_i}:{units}".encode()).digest()
    dust = (units + digest[0] % 5) // 5 if units else 0
    tomes = min(6, (units + digest[1] % 80) // 80) if units else 0
    # A different phase deliberately prevents permanent tome/essence lockstep.
    essence = min(6, (units + 40 + digest[2] % 40) // 80) if units else 0
    return {"gold": units * round(2 + stage_i * .12), "salvage_dust": min(units, dust),
            "chest_xp": min(48, units // 10), "skill_tomes": tomes,
            "companion_essence": essence, "refinement_ore": min(2, units // 240),
            "premium_crystals": 0, "reward_units": units}


def public_status(player: Mapping[str, Any], server_time: int) -> dict:
    snapshot = normalize_snapshot(player.get("offline_unclaimed_json"))
    if snapshot:
        total = max(0, snapshot["ended_at"] - snapshot["started_at"])
        rewarded = snapshot["rewarded_seconds"]
        stage, chest, seed = snapshot["stage"], snapshot["chest_level"], f"{snapshot['started_at']}:{snapshot['ended_at']}"
    else:
        timing = elapsed(player.get("last_active_at"), server_time)
        total, rewarded = timing["offline_seconds_total"], timing["offline_seconds_rewarded"]
        stage, chest, seed = player.get("stage", 1), player.get("chest_level", 1), f"{player.get('last_active_at',0)}:{server_time}"
    preview = rewards(stage, chest, rewarded, seed=seed)
    return {"server_time": int(server_time), "last_active_at": max(0, _integer(player.get("last_active_at"))),
            "offline_seconds_total": total, "offline_seconds_rewarded": rewarded,
            "offline_cap_seconds": OFFLINE_CAP_SECONDS, "cap_reached": rewarded >= OFFLINE_CAP_SECONDS,
            "claimable": preview["reward_units"] > 0, "estimated_rewards": preview,
            "claim_version": max(1, _integer(player.get("offline_claim_version"), 1))}
