"""Deterministic, database-agnostic combat effect engine.

The API adapts persisted player rows to this module.  Nothing here mutates a player
or knows about SQLite, which keeps effect evaluation safe to repeat for responses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from threading import RLock
import copy
import time
import uuid
from typing import Any, Callable, Iterable


class CombatEvent(str, Enum):
    BATTLE_START = "battle_start"
    BEFORE_NORMAL_ATTACK = "before_normal_attack"
    AFTER_NORMAL_ATTACK = "after_normal_attack"
    BEFORE_SKILL = "before_skill"
    AFTER_SKILL = "after_skill"
    POISON_TICK = "poison_tick"
    BEFORE_RECEIVE_DAMAGE = "before_receive_damage"
    AFTER_RECEIVE_DAMAGE = "after_receive_damage"
    SHIELD_CREATED = "shield_created"
    SHIELD_BROKEN = "shield_broken"
    ENEMY_KILLED = "enemy_killed"
    BOSS_KILLED = "boss_killed"
    BATTLE_END = "battle_end"


class RefreshMode(str, Enum):
    REFRESH = "refresh"
    EXTEND = "extend"
    KEEP = "keep"


@dataclass
class CombatStats:
    damage_multiplier: float = 1.0
    max_hp_multiplier: float = 1.0
    attack_speed_multiplier: float = 1.0
    crit_chance_bonus: float = 0.0
    crit_damage_bonus: float = 0.0
    skill_damage_multiplier: float = 1.0
    companion_damage_multiplier: float = 1.0
    cooldown_multiplier: float = 1.0
    incoming_damage_multiplier: float = 1.0
    healing_multiplier: float = 1.0
    boss_damage_multiplier: float = 1.0
    dodge_chance: float = 0.0
    combo_chance: float = 0.0
    counter_chance: float = 0.0
    physical_resistance: float = 0.0
    nature_resistance: float = 0.0
    poison_resistance: float = 0.0
    arcane_resistance: float = 0.0

    def apply(self, modifiers: dict[str, float]) -> None:
        additive = {"crit_chance_bonus", "crit_damage_bonus", "physical_resistance",
                    "nature_resistance", "poison_resistance", "arcane_resistance"}
        for name, value in modifiers.items():
            if not hasattr(self, name):
                raise ValueError(f"Unknown combat stat: {name}")
            current = float(getattr(self, name))
            setattr(self, name, current + value if name in additive else current * value)
        self.crit_chance_bonus = min(100.0, max(0.0, self.crit_chance_bonus))
        self.crit_damage_bonus = min(200.0, max(0.0, self.crit_damage_bonus))
        self.attack_speed_multiplier = min(1.5, max(0.0, self.attack_speed_multiplier))
        self.skill_damage_multiplier = min(2.5, max(0.0, self.skill_damage_multiplier))
        self.companion_damage_multiplier = min(2.5, max(0.0, self.companion_damage_multiplier))
        self.boss_damage_multiplier = min(2.0, max(0.0, self.boss_damage_multiplier))
        self.healing_multiplier = min(2.0, max(0.0, self.healing_multiplier))
        self.incoming_damage_multiplier = max(0.0, self.incoming_damage_multiplier)
        self.dodge_chance = min(50.0, max(0.0, self.dodge_chance))
        self.combo_chance = min(50.0, max(0.0, self.combo_chance))
        self.counter_chance = min(50.0, max(0.0, self.counter_chance))
        for name in ("physical_resistance", "nature_resistance", "poison_resistance", "arcane_resistance"):
            setattr(self, name, min(.50, max(-.50, float(getattr(self, name)))))


@dataclass
class TemporaryEffect:
    effect_id: str
    duration_seconds: float
    remaining_duration: float
    stack_count: int = 1
    max_stacks: int = 1
    refresh_mode: str = RefreshMode.REFRESH.value
    source_type: str = "system"
    source_id: str = ""
    modifiers: dict[str, float] = field(default_factory=dict)

    def advance(self, seconds: float) -> bool:
        self.remaining_duration = max(0.0, self.remaining_duration - max(0.0, seconds))
        return self.remaining_duration > 0

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CombatContext:
    current_hp: int
    max_hp: int
    enemy_hp: int
    enemy_type: str = "normal"
    elapsed_time: float = 0.0
    active_shield: int = 0
    cooldowns: dict[str, float] = field(default_factory=dict)
    temporary_effects: list[TemporaryEffect] = field(default_factory=list)
    damage_breakdown: dict[str, int] = field(default_factory=dict)
    healing_breakdown: dict[str, int] = field(default_factory=dict)

    def add_temporary_effect(self, effect: TemporaryEffect) -> bool:
        if effect.effect_id not in TEMPORARY_EFFECT_DEFINITIONS:
            return False
        existing = next((item for item in self.temporary_effects if (
            item.effect_id, item.source_type, item.source_id
        ) == (effect.effect_id, effect.source_type, effect.source_id)), None)
        if existing is None:
            effect.stack_count = min(max(1, effect.stack_count), max(1, effect.max_stacks))
            self.temporary_effects.append(effect)
            return True
        existing.stack_count = min(existing.max_stacks, existing.stack_count + effect.stack_count)
        if existing.refresh_mode == RefreshMode.REFRESH.value:
            existing.remaining_duration = existing.duration_seconds
        elif existing.refresh_mode == RefreshMode.EXTEND.value:
            existing.remaining_duration += effect.duration_seconds
        return True

    def advance(self, seconds: float) -> None:
        self.elapsed_time += max(0.0, seconds)
        self.temporary_effects = [effect for effect in self.temporary_effects if effect.advance(seconds)]


@dataclass(frozen=True)
class EffectDefinition:
    effect_id: str
    source_type: str
    priority: int = 100
    modifiers_per_level: dict[str, float] = field(default_factory=dict)
    events: tuple[CombatEvent, ...] = ()
    description: str = ""
    public_extra: Callable[[int], dict[str, Any]] | None = None
    custom_per_level: dict[str, float] = field(default_factory=dict)


COMPANION_EFFECTS: dict[str, EffectDefinition] = {
    "forest_sprite": EffectDefinition("forest_sprite", "companion", modifiers_per_level={"damage_multiplier": .013}, description="Увеличивает итоговый урон героя на 1,3% за уровень."),
    "baby_slime": EffectDefinition("baby_slime", "companion", modifiers_per_level={"max_hp_multiplier": .015}, description="Увеличивает максимальный HP героя на 1,5% за уровень."),
    "spore_beetle": EffectDefinition("spore_beetle", "companion", modifiers_per_level={"attack_speed_multiplier": .0065}, events=(CombatEvent.BEFORE_NORMAL_ATTACK,), custom_per_level={"extra_attack_ratio": .028}, description="Добавляет к обычной атаке 2,8% базового урона героя и 0,65% скорости обычной атаки за уровень."),
    "mushroom_owl": EffectDefinition("mushroom_owl", "companion", modifiers_per_level={"cooldown_multiplier": -.015}, description="Снижает время перезарядки всех навыков на 1,5% за уровень (до 30%)."),
    "thorn_wolf": EffectDefinition("thorn_wolf", "companion", modifiers_per_level={"crit_chance_bonus": .75, "crit_damage_bonus": 1.0}, description="Даёт +0,75 п.п. к шансу крита и +1% к критическому урону за уровень.", public_extra=lambda level: {"crit_damage_bonus": round(level * 1.0, 4)}),
    "ancient_entling": EffectDefinition("ancient_entling", "companion", events=(CombatEvent.ENEMY_KILLED, CombatEvent.BOSS_KILLED), custom_per_level={"victory_healing_ratio": .006}, description="Восстанавливает 0,6% максимального HP за уровень после победы и вдвое больше после босса."),
    "crystal_moth": EffectDefinition("crystal_moth", "companion", events=(CombatEvent.AFTER_NORMAL_ATTACK,), custom_per_level={"crystal_moth_ratio": .018}, description="После обычной атаки наносит 1,8% arcane-урона за уровень."),
    "moss_turtle": EffectDefinition("moss_turtle", "companion", modifiers_per_level={"nature_resistance": .008, "poison_resistance": .005}, description="Даёт сопротивление природе и яду."),
}

SKILL_EFFECTS: dict[str, dict[str, Any]] = {
    "spore_strike": {"event": CombatEvent.BEFORE_SKILL, "damage_multiplier": 2.0, "cooldown_seconds": 8.0},
    "mushroom_shield": {"event": CombatEvent.SHIELD_CREATED, "hp_ratio": .30, "cooldown_seconds": 15.0},
    "poison_cloud": {"event": CombatEvent.POISON_TICK, "damage_multiplier": .45, "duration_seconds": 5.0, "tick_seconds": 1.0, "cooldown_seconds": 20.0},
    "thorn_burst": {"event": CombatEvent.BEFORE_SKILL, "damage_multiplier": 1.25, "growth": .06, "cooldown_seconds": 7.0},
    "arcane_echo": {"event": CombatEvent.BEFORE_SKILL, "damage_multipliers": (.80, .55), "growth": .05, "cooldown_seconds": 9.0},
}

# New temporary effects must be registered before persisted/client supplied IDs are accepted.
TEMPORARY_EFFECT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "poison_cloud": {"events": (CombatEvent.POISON_TICK,)},
    "mushroom_owl_repeat": {"events": (CombatEvent.BEFORE_SKILL,)},
    "poison_completion": {"events": (CombatEvent.POISON_TICK,)},
    "shield_mitigation": {"events": (CombatEvent.BEFORE_RECEIVE_DAMAGE,)},
}


@dataclass
class BattleState:
    """Short-lived state for exactly one spawned enemy."""

    identity: tuple[Any, ...]
    temporary_effects: list[TemporaryEffect] = field(default_factory=list)
    poison_ticks: int = 0
    poison_owl_bonus: float = 0.0
    poison_stack_bonus: float = 0.0
    poison_instance_id: str | None = None
    processed_poison_ticks: set[int] = field(default_factory=set)
    shield_capacity_multiplier: float = 1.0
    last_seen: float = 0.0

    def effect(self, effect_id: str, source_id: str = "") -> TemporaryEffect | None:
        return next((effect for effect in self.temporary_effects if effect.effect_id == effect_id and (not source_id or effect.source_id == source_id)), None)

    def stacks(self, effect_id: str, source_id: str = "") -> int:
        effect = self.effect(effect_id, source_id)
        return effect.stack_count if effect else 0


class BattleStateStore:
    """Bounded, process-local battle state. Entries never persist to the database."""

    def __init__(self, max_entries: int = 4096, ttl_seconds: float = 3600.0) -> None:
        self.max_entries = max(1, max_entries)
        self.ttl_seconds = max(1.0, ttl_seconds)
        self._states: dict[int, BattleState] = {}
        self._lock = RLock()

    def _prune(self, now: float) -> None:
        expired = [key for key, state in self._states.items() if now - state.last_seen > self.ttl_seconds]
        for key in expired:
            self._states.pop(key, None)
        if len(self._states) >= self.max_entries:
            oldest = sorted(self._states, key=lambda key: self._states[key].last_seen)
            for key in oldest[:len(self._states) - self.max_entries + 1]:
                self._states.pop(key, None)

    def get(self, player_id: int, identity: tuple[Any, ...], now: float | None = None) -> BattleState:
        current_time = time.time() if now is None else now
        with self._lock:
            self._prune(current_time)
            state = self._states.get(player_id)
            if state is None or state.identity != identity:
                state = BattleState(identity=identity, last_seen=current_time)
                self._states[player_id] = state
            state.last_seen = current_time
            state.temporary_effects = [effect for effect in state.temporary_effects if effect.remaining_duration <= 0 or effect.remaining_duration > current_time]
            return state

    def peek(self, player_id: int, identity: tuple[Any, ...], now: float | None = None) -> BattleState | None:
        current_time = time.time() if now is None else now
        with self._lock:
            state = self._states.get(player_id)
            if state is None or state.identity != identity:
                return None
            state.temporary_effects = [effect for effect in state.temporary_effects if effect.remaining_duration <= 0 or effect.remaining_duration > current_time]
            return state

    def reset(self, player_id: int) -> None:
        with self._lock:
            self._states.pop(player_id, None)

    def snapshot(self, player_id: int) -> BattleState | None:
        with self._lock:
            return copy.deepcopy(self._states.get(player_id))

    def restore(self, player_id: int, state: BattleState | None) -> None:
        with self._lock:
            if state is None:
                self._states.pop(player_id, None)
            else:
                self._states[player_id] = state

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)

    @staticmethod
    def _add_stack(state: BattleState, effect_id: str, source_id: str, max_stacks: int) -> int:
        effect = state.effect(effect_id, source_id)
        if effect is None:
            effect = TemporaryEffect(effect_id, 0, 0, 1, max_stacks, source_type="combat", source_id=source_id)
            state.temporary_effects.append(effect)
        else:
            effect.stack_count = min(max_stacks, effect.stack_count + 1)
        return effect.stack_count

    def use_skill(self, state: BattleState, skill_id: str, owl_active: bool) -> tuple[int, float]:
        if not owl_active:
            return 0, 0.0
        repeats = state.stacks("mushroom_owl_repeat", skill_id)
        bonus = min(5, repeats) * .10
        # Store applications (up to six) so public repeat stacks remain 0 after
        # the first cast and reach five after the sixth cast.
        self._add_stack(state, "mushroom_owl_repeat", skill_id, 6)
        return min(5, repeats), bonus

    def begin_poison(self, state: BattleState, owl_active: bool) -> tuple[int, float, int, float]:
        owl_stacks, owl_bonus = self.use_skill(state, "poison_cloud", owl_active)
        completion_stacks = state.stacks("poison_completion", "poison_cloud")
        state.poison_ticks = 0
        state.poison_owl_bonus = owl_bonus
        state.poison_stack_bonus = completion_stacks * .10
        state.poison_instance_id = uuid.uuid4().hex
        state.processed_poison_ticks.clear()
        return owl_stacks, owl_bonus, completion_stacks, state.poison_stack_bonus

    def claim_poison_tick(self, state: BattleState, instance_id: str, tick_index: int) -> bool:
        if state.poison_instance_id != instance_id or tick_index < 1 or tick_index > 5:
            return False
        if tick_index in state.processed_poison_ticks:
            return False
        state.processed_poison_ticks.add(tick_index)
        return True

    def begin_shield(self, state: BattleState, owl_active: bool) -> tuple[int, float]:
        stacks, bonus = self.use_skill(state, "mushroom_shield", owl_active)
        state.shield_capacity_multiplier = 1.0 + bonus
        return stacks, bonus

    def record_poison_ticks(self, state: BattleState, ticks: int) -> bool:
        before = state.poison_ticks
        state.poison_ticks = min(5, state.poison_ticks + max(0, ticks))
        if before < 5 and state.poison_ticks == 5:
            self._add_stack(state, "poison_completion", "poison_cloud", 3)
            return True
        return False

    def break_shield(self, state: BattleState, now: float) -> None:
        effect = state.effect("shield_mitigation", "mushroom_shield")
        if effect is None:
            state.temporary_effects.append(TemporaryEffect(
                "shield_mitigation", 3.0, now + 3.0, source_type="skill",
                source_id="mushroom_shield", modifiers={"incoming_damage_multiplier": .90},
            ))
        else:
            effect.remaining_duration = now + 3.0

    def mitigation_remaining(self, state: BattleState | None, now: float) -> float:
        effect = state.effect("shield_mitigation", "mushroom_shield") if state else None
        return max(0.0, effect.remaining_duration - now) if effect else 0.0

    def incoming_damage_multiplier(self, state: BattleState | None, now: float) -> float:
        stats = CombatStats()
        for effect in sorted(
            state.temporary_effects if state else [], key=lambda item: (item.effect_id, item.source_id)
        ):
            if effect.remaining_duration > now and effect.modifiers:
                stats.apply(effect.modifiers)
        return stats.incoming_damage_multiplier


class CombatEffectEngine:
    """Combines sources and dispatches handlers in a stable order."""

    def __init__(self) -> None:
        self._handlers: dict[CombatEvent, list[tuple[int, str, Callable]]] = {event: [] for event in CombatEvent}

    def register_handler(self, event: CombatEvent, effect_id: str, handler: Callable, priority: int = 100) -> None:
        self._handlers[event].append((priority, effect_id, handler))
        self._handlers[event].sort(key=lambda item: (item[0], item[1]))

    def dispatch(self, event: CombatEvent, context: CombatContext, payload: dict[str, Any] | None = None) -> list[str]:
        order = []
        for _, effect_id, handler in self._handlers[event]:
            handler(context, payload or {})
            order.append(effect_id)
        return order

    @staticmethod
    def combine(sources: Iterable[tuple[EffectDefinition, int]]) -> tuple[CombatStats, dict[str, float]]:
        stats = CombatStats()
        custom: dict[str, float] = {}
        for definition, level in sorted(sources, key=lambda item: (item[0].priority, item[0].effect_id)):
            modifiers = {}
            for name, per_level in definition.modifiers_per_level.items():
                if name == "cooldown_multiplier":
                    # Reduction is additive and capped at 30%, preserving the published rule.
                    custom["cooldown_reduction"] = custom.get("cooldown_reduction", 0.0) + (-per_level * level)
                elif name.endswith("_bonus"):
                    modifiers[name] = per_level * level
                else:
                    modifiers[name] = 1.0 + per_level * level
            stats.apply(modifiers)
            for name, per_level in definition.custom_per_level.items():
                custom[name] = custom.get(name, 0.0) + per_level * level
        stats.cooldown_multiplier = 1.0 - min(.30, custom.pop("cooldown_reduction", 0.0))
        return stats, custom


def active_companion_sources(equipped: Iterable[str | None], collection: dict[str, dict], unlocked_count: int) -> list[tuple[EffectDefinition, int]]:
    sources = []
    for companion_id in list(equipped)[:max(0, unlocked_count)]:
        entry = collection.get(companion_id or "", {})
        definition = COMPANION_EFFECTS.get(companion_id or "")
        if definition is not None and bool(entry.get("owned")):
            sources.append((definition, max(1, int(entry.get("level", 1)))))
    return sources


def public_active_effects(sources: Iterable[tuple[EffectDefinition, int]], names: dict[str, str]) -> list[dict[str, Any]]:
    result = []
    for definition, level in sources:
        item = {"companion_id": definition.effect_id, "name": names.get(definition.effect_id, definition.effect_id), "level": level, "description": definition.description}
        if definition.public_extra:
            item.update(definition.public_extra(level))
        result.append(item)
    return result
