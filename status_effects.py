"""Battle-local status effects shared by combat routes.

The module is deliberately independent from FastAPI and persistence.  Callers
snapshot the store next to their database transaction and restore it on error.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from threading import RLock
import copy
import time
from typing import Any, Iterable


EFFECT_TYPES = frozenset({
    "damage_over_time", "resistance_debuff", "slow", "root", "silence",
    "damage_reduction", "healing_over_time",
})
STACK_RULES = frozenset({"refresh", "stack", "replace", "strongest", "independent"})
NEGATIVE_EFFECT_TYPES = frozenset({
    "damage_over_time", "resistance_debuff", "slow", "root", "silence",
})
CLEANSE_PRIORITY = ("silence", "root", "slow", "resistance_debuff", "damage_over_time")


@dataclass
class StatusEffect:
    effect_id: str
    effect_type: str
    source_id: str
    source_kind: str
    target: str
    battle_identity: str
    damage_type: str | None = None
    applied_at: float = 0.0
    expires_at: float = 0.0
    duration_seconds: float = 0.0
    tick_interval_seconds: float = 0.0
    next_tick_at: float = 0.0
    max_ticks: int = 0
    processed_ticks: int = 0
    stack_count: int = 1
    max_stacks: int = 1
    stack_rule: str = "refresh"
    potency: float = 0.0
    snapshot: dict[str, Any] = field(default_factory=dict)
    control_flags: dict[str, Any] = field(default_factory=dict)
    dispellable: bool = True
    active: bool = True

    def __post_init__(self) -> None:
        if self.effect_type not in EFFECT_TYPES:
            raise ValueError(f"unsupported effect_type: {self.effect_type}")
        if self.stack_rule not in STACK_RULES:
            raise ValueError(f"unsupported stack_rule: {self.stack_rule}")
        if self.target not in {"hero", "enemy"}:
            raise ValueError("target must be hero or enemy")
        self.effect_id = str(self.effect_id)
        self.battle_identity = str(self.battle_identity)
        self.stack_count = max(1, min(int(self.stack_count), max(1, int(self.max_stacks))))
        self.max_ticks = max(0, int(self.max_ticks))
        self.processed_ticks = max(0, min(int(self.processed_ticks), self.max_ticks or int(self.processed_ticks)))

    @property
    def ticks_remaining(self) -> int:
        return max(0, self.max_ticks - self.processed_ticks)

    def public(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        return {
            "effect_id": self.effect_id, "effect_type": self.effect_type,
            "source_id": self.source_id, "source_kind": self.source_kind,
            "target": self.target, "damage_type": self.damage_type,
            "stack_count": self.stack_count, "potency": self.potency,
            "remaining_seconds": round(max(0.0, self.expires_at - now), 3),
            "next_tick_in": (round(max(0.0, self.next_tick_at - now), 3)
                             if self.next_tick_at > 0 and self.ticks_remaining else None),
            "ticks_remaining": self.ticks_remaining, "dispellable": self.dispellable,
            "control_flags": copy.deepcopy(self.control_flags),
        }


@dataclass
class StatusMutation:
    effect: StatusEffect
    action: str
    removed: list[StatusEffect] = field(default_factory=list)


class StatusEffectStore:
    """Bounded, deterministic, process-local effect storage."""

    def __init__(self, max_players: int = 4096, max_effects: int = 32,
                 ttl_seconds: float = 3600.0) -> None:
        self.max_players = max(1, int(max_players))
        self.max_effects = max(1, int(max_effects))
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._effects: dict[int, list[StatusEffect]] = {}
        self._seen: dict[int, float] = {}
        self._sequence = 0
        self._lock = RLock()

    def _prune(self, now: float) -> None:
        stale = [pid for pid, seen in self._seen.items() if now - seen >= self.ttl_seconds]
        for pid in stale:
            self._effects.pop(pid, None)
            self._seen.pop(pid, None)
        while len(self._effects) > self.max_players:
            victim = min(self._effects, key=lambda pid: (self._seen.get(pid, 0), pid))
            self._effects.pop(victim, None)
            self._seen.pop(victim, None)

    def _touch(self, player_id: int, now: float) -> None:
        self._seen[player_id] = now

    def next_id(self, player_id: int, source_id: str, now: float | None = None) -> str:
        with self._lock:
            self._sequence += 1
            stamp = int((time.time() if now is None else now) * 1_000_000)
            return f"{stamp:016d}:{int(player_id):020d}:{self._sequence:010d}:{source_id}"

    def list(self, player_id: int, battle_identity: str, now: float | None = None,
             *, target: str | None = None, include_expired: bool = False) -> list[StatusEffect]:
        now = time.time() if now is None else float(now)
        player_id, identity = int(player_id), str(battle_identity)
        with self._lock:
            self._prune(now)
            self._touch(player_id, now)
            items = [effect for effect in self._effects.get(player_id, [])
                     if effect.battle_identity == identity and effect.active
                     and (target is None or effect.target == target)
                     and (include_expired or effect.expires_at > now)]
            return sorted(items, key=lambda effect: effect.effect_id)

    def clear(self, player_id: int | None = None) -> None:
        with self._lock:
            if player_id is None:
                self._effects.clear(); self._seen.clear()
            else:
                self._effects.pop(int(player_id), None); self._seen.pop(int(player_id), None)

    reset = clear

    def clear_identity(self, player_id: int, battle_identity: str) -> list[StatusEffect]:
        player_id, identity = int(player_id), str(battle_identity)
        with self._lock:
            removed = [e for e in self._effects.get(player_id, []) if e.battle_identity == identity]
            self._effects[player_id] = [e for e in self._effects.get(player_id, []) if e.battle_identity != identity]
            return sorted(removed, key=lambda e: e.effect_id)

    def synchronize_identity(self, player_id: int, battle_identity: str, now: float | None = None) -> list[StatusEffect]:
        now = time.time() if now is None else float(now)
        player_id, identity = int(player_id), str(battle_identity)
        with self._lock:
            self._prune(now); self._touch(player_id, now)
            removed = [e for e in self._effects.get(player_id, []) if e.battle_identity != identity]
            self._effects[player_id] = [e for e in self._effects.get(player_id, []) if e.battle_identity == identity]
            return sorted(removed, key=lambda e: e.effect_id)

    def expire(self, player_id: int, battle_identity: str, now: float | None = None) -> list[StatusEffect]:
        now = time.time() if now is None else float(now)
        with self._lock:
            expired = [e for e in self._effects.get(int(player_id), [])
                       if e.battle_identity == str(battle_identity) and e.active and e.expires_at <= now]
            for effect in expired: effect.active = False
            return sorted(expired, key=lambda e: e.effect_id)

    def apply(self, player_id: int, effect: StatusEffect, now: float | None = None) -> StatusMutation:
        now = time.time() if now is None else float(now)
        player_id = int(player_id)
        with self._lock:
            self._prune(now); self._touch(player_id, now)
            bucket = self._effects.setdefault(player_id, [])
            self._prune(now)
            bucket = self._effects.setdefault(player_id, bucket)
            # Identity changes are a hard battle boundary.
            bucket[:] = [item for item in bucket if item.battle_identity == effect.battle_identity]
            same = [item for item in bucket if item.active and item.target == effect.target
                    and item.source_id == effect.source_id and item.effect_type == effect.effect_type]
            removed: list[StatusEffect] = []
            if effect.stack_rule == "independent":
                if len(same) >= max(1, effect.max_stacks):
                    victim = min(same, key=lambda item: (item.applied_at, item.effect_id))
                    victim.active = False; removed.append(victim)
                bucket.append(effect); action = "applied"
            elif same:
                current = min(same, key=lambda item: item.effect_id)
                if effect.stack_rule == "refresh":
                    current.applied_at = effect.applied_at; current.expires_at = effect.expires_at
                    current.duration_seconds = effect.duration_seconds; current.potency = effect.potency
                    current.snapshot = copy.deepcopy(effect.snapshot); current.control_flags = copy.deepcopy(effect.control_flags)
                    action = "refreshed"; effect = current
                elif effect.stack_rule == "stack":
                    current.stack_count = min(current.max_stacks, current.stack_count + effect.stack_count)
                    current.expires_at = max(current.expires_at, effect.expires_at); action = "refreshed"; effect = current
                elif effect.stack_rule == "strongest" and current.potency > effect.potency:
                    current.expires_at = max(current.expires_at, effect.expires_at); action = "refreshed"; effect = current
                else:  # replace, or stronger strongest
                    for item in same: item.active = False; removed.append(item)
                    bucket.append(effect); action = "applied"
            else:
                bucket.append(effect); action = "applied"
            active = [item for item in bucket if item.active]
            if len(active) > self.max_effects:
                if effect in active:
                    effect.active = False; bucket.remove(effect)
                raise OverflowError(f"maximum {self.max_effects} active effects per player")
            return StatusMutation(effect, action, sorted(removed, key=lambda item: item.effect_id))

    def due_ticks(self, player_id: int, battle_identity: str, now: float | None = None,
                  limit: int = 32, target: str | None = None) -> list[tuple[StatusEffect, int, float]]:
        """Claim due ticks atomically, ordered by time, effect id, tick index."""
        now = time.time() if now is None else float(now)
        events: list[tuple[float, str, int, StatusEffect]] = []
        with self._lock:
            for effect in self.list(player_id, battle_identity, now, include_expired=True):
                if target is not None and effect.target != target:
                    continue
                if effect.effect_type not in {"damage_over_time", "healing_over_time"}:
                    continue
                interval = float(effect.tick_interval_seconds)
                index = effect.processed_ticks + 1
                due_at = effect.next_tick_at
                while interval > 0 and index <= effect.max_ticks and due_at <= now:
                    events.append((due_at, effect.effect_id, index, effect))
                    index += 1
                    due_at += interval
            events.sort(key=lambda row: (row[0], row[1], row[2]))
            claimed = events[:max(0, int(limit))]
            by_effect: dict[str, tuple[StatusEffect, int]] = {}
            for _, _, index, effect in claimed:
                previous = by_effect.get(effect.effect_id)
                if previous is None or index > previous[1]:
                    by_effect[effect.effect_id] = (effect, index)
            for effect, last_index in by_effect.values():
                claimed_count = last_index - effect.processed_ticks
                effect.processed_ticks = last_index
                effect.next_tick_at += claimed_count * effect.tick_interval_seconds
            return [(effect, index, due_at) for due_at, _, index, effect in claimed]

    def remove(self, player_id: int, effect_id: str) -> StatusEffect | None:
        with self._lock:
            for effect in self._effects.get(int(player_id), []):
                if effect.effect_id == effect_id and effect.active:
                    effect.active = False
                    return effect
        return None

    def cleanse_one(self, player_id: int, battle_identity: str, target: str = "hero",
                    now: float | None = None) -> StatusEffect | None:
        effects = self.list(player_id, battle_identity, now, target=target)
        for kind in CLEANSE_PRIORITY:
            candidate = next((e for e in effects if e.effect_type == kind and e.dispellable
                              and e.effect_type in NEGATIVE_EFFECT_TYPES), None)
            if candidate:
                return self.remove(player_id, candidate.effect_id)
        return None

    def snapshot(self, player_id: int | None = None) -> Any:
        with self._lock:
            if player_id is None:
                return copy.deepcopy((self._effects, self._seen, self._sequence))
            pid = int(player_id)
            return copy.deepcopy((self._effects.get(pid), self._seen.get(pid), self._sequence))

    def restore(self, player_id: int | None, snapshot: Any) -> None:
        with self._lock:
            if player_id is None:
                self._effects, self._seen, self._sequence = copy.deepcopy(snapshot)
                return
            pid = int(player_id); effects, seen, sequence = copy.deepcopy(snapshot)
            if effects is None: self._effects.pop(pid, None)
            else: self._effects[pid] = effects
            if seen is None: self._seen.pop(pid, None)
            else: self._seen[pid] = seen
            self._sequence = sequence


def status_effect(*, effect_id: str, effect_type: str, source_id: str,
                  battle_identity: str, now: float, duration_seconds: float,
                  target: str = "enemy", source_kind: str = "skill", **values: Any) -> StatusEffect:
    """Small safe constructor used by routes and unit tests."""
    duration = max(0.0, float(duration_seconds))
    return StatusEffect(effect_id=effect_id, effect_type=effect_type,
                        source_id=source_id, source_kind=source_kind, target=target,
                        battle_identity=str(battle_identity), applied_at=float(now),
                        expires_at=float(now) + duration, duration_seconds=duration, **values)
