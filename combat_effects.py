"""Deterministic, database-agnostic combat effect engine.

The API adapts persisted player rows to this module.  Nothing here mutates a player
or knows about SQLite, which keeps effect evaluation safe to repeat for responses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
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

    def apply(self, modifiers: dict[str, float]) -> None:
        additive = {"crit_chance_bonus", "crit_damage_bonus"}
        for name, value in modifiers.items():
            if not hasattr(self, name):
                raise ValueError(f"Unknown combat stat: {name}")
            current = float(getattr(self, name))
            setattr(self, name, current + value if name in additive else current * value)
        self.crit_chance_bonus = min(100.0, max(0.0, self.crit_chance_bonus))
        self.incoming_damage_multiplier = max(0.0, self.incoming_damage_multiplier)


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
    "spore_beetle": EffectDefinition("spore_beetle", "companion", events=(CombatEvent.BEFORE_NORMAL_ATTACK,), custom_per_level={"extra_attack_ratio": .028}, description="Добавляет к обычной атаке 2,8% базового урона героя за уровень."),
    "mushroom_owl": EffectDefinition("mushroom_owl", "companion", modifiers_per_level={"cooldown_multiplier": -.015}, description="Снижает время перезарядки всех навыков на 1,5% за уровень (до 30%)."),
    "thorn_wolf": EffectDefinition("thorn_wolf", "companion", modifiers_per_level={"crit_chance_bonus": .75, "crit_damage_bonus": 1.0}, description="Даёт +0,75 п.п. к шансу крита и +1% к критическому урону за уровень.", public_extra=lambda level: {"crit_damage_bonus": round(level * 1.0, 4)}),
    "ancient_entling": EffectDefinition("ancient_entling", "companion", events=(CombatEvent.ENEMY_KILLED, CombatEvent.BOSS_KILLED), custom_per_level={"victory_healing_ratio": .006}, description="Восстанавливает 0,6% максимального HP за уровень после победы и вдвое больше после босса."),
}

SKILL_EFFECTS: dict[str, dict[str, Any]] = {
    "spore_strike": {"event": CombatEvent.BEFORE_SKILL, "damage_multiplier": 2.0, "cooldown_seconds": 8.0},
    "mushroom_shield": {"event": CombatEvent.SHIELD_CREATED, "hp_ratio": .30, "cooldown_seconds": 15.0},
    "poison_cloud": {"event": CombatEvent.POISON_TICK, "damage_multiplier": .45, "duration_seconds": 5.0, "tick_seconds": 1.0, "cooldown_seconds": 20.0},
}

# New temporary effects must be registered before persisted/client supplied IDs are accepted.
TEMPORARY_EFFECT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "poison_cloud": {"events": (CombatEvent.POISON_TICK,)},
}


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
