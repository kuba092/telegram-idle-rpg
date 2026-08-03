"""Server-owned resolution of damage and battle transitions.

The resolver is deliberately independent of FastAPI.  Its caller owns one open
database transaction and supplies the campaign-specific victory callback, so
damage, rewards, healing and spawning the next enemy commit or roll back as a
single unit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable

from combat_effects import CombatContext
from damage_types import DAMAGE_TYPES, normalize_resistances, resistance_breakdown


@dataclass
class BattleResult:
    damage_source: str
    attack_source: str
    raw_damage: int
    damage_type: str = "physical"
    resistance_before: float = 0.0
    resistance_after_penetration: float = 0.0
    damage_before_resistance: int = 0
    damage_after_resistance: int = 0
    resistance_reduction: int = 0
    penetration_used: float = 0.0
    crit_metadata: dict[str, Any] | None = None
    final_damage: int = 0
    enemy_hp_before: int = 0
    enemy_hp_after: int = 0
    lethal: bool = False
    stale_battle: bool = False
    skipped_after_kill: bool = False
    reward_granted: bool = False
    companion_healing: int = 0
    stage_advanced: bool = False
    boss_started: bool = False
    boss_defeated: bool = False
    next_battle_identity: str | None = None
    temporary_effects_cleared: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


class CombatResolution:
    """Ordered results for one HTTP action."""

    def __init__(self) -> None:
        self.events: list[BattleResult] = []
        self.killed = False

    def add(self, event: BattleResult) -> BattleResult:
        self.events.append(event)
        self.killed = self.killed or event.lethal
        return event

    def skip(self, damage_source: str, attack_source: str, raw_damage: int,
             damage_type: str = "physical") -> BattleResult:
        return self.add(BattleResult(
            damage_source=damage_source, attack_source=attack_source,
            raw_damage=max(0, round(raw_damage)), damage_type=damage_type,
            damage_before_resistance=max(0, round(raw_damage)), skipped_after_kill=True,
        ))

    def public(self) -> dict[str, Any]:
        lethal = next((event for event in self.events if event.lethal), None)
        return {
            "events": [event.public() for event in self.events],
            "lethal_source": lethal.damage_source if lethal else None,
            "total_damage": sum(event.final_damage for event in self.events),
            "enemy_defeated": lethal is not None,
            "reward_granted": bool(lethal and lethal.reward_granted),
            "transition_completed": bool(lethal and lethal.next_battle_identity),
            "next_battle_identity": lethal.next_battle_identity if lethal else None,
        }


class CombatResolver:
    """Apply one damage event inside the caller's database transaction."""

    def __init__(
        self,
        connection: Any,
        player: dict[str, Any],
        battle_identity: str,
        current_identity: Callable[[dict[str, Any]], str],
        victory_handler: Callable[[Any, dict[str, Any], bool, CombatContext], dict[str, Any]],
        resolution: CombatResolution | None = None,
    ) -> None:
        self.connection = connection
        self.player = player
        self.battle_identity = battle_identity
        self.current_identity = current_identity
        self.victory_handler = victory_handler
        self.resolution = resolution or CombatResolution()

    def resolve(
        self,
        *,
        damage_source: str,
        raw_damage: int,
        attack_source: str,
        crit_metadata: dict[str, Any] | None,
        boss: bool,
        context: CombatContext,
        damage_type: str = "physical",
        penetration: float = 0.0,
    ) -> BattleResult:
        raw_damage = max(0, round(raw_damage))
        damage_type = damage_type if damage_type in DAMAGE_TYPES else "physical"
        if self.resolution.killed:
            return self.resolution.skip(damage_source, attack_source, raw_damage, damage_type)

        if self.battle_identity != self.current_identity(self.player):
            return self.resolution.add(BattleResult(
                damage_source=damage_source, attack_source=attack_source,
                raw_damage=raw_damage, stale_battle=True,
                damage_type=damage_type, damage_before_resistance=raw_damage,
            ))

        maximum = max(0, int(self.player.get("enemy_max_hp", 0)))
        before = min(maximum, max(0, int(self.player.get("enemy_hp", 0))))
        if before == 0:
            return self.resolution.add(BattleResult(
                damage_source=damage_source, attack_source=attack_source,
                raw_damage=raw_damage, stale_battle=True,
                damage_type=damage_type, damage_before_resistance=raw_damage,
                enemy_hp_before=0, enemy_hp_after=0,
            ))
        resistances = normalize_resistances(self.player.get("enemy_resistances_json"))
        breakdown = resistance_breakdown(
            raw_damage, damage_type, resistances.get(damage_type, 0), penetration,
        )
        final_damage = min(before, breakdown["damage_after_resistance"])
        after = min(maximum, max(0, before - final_damage))
        self.player["enemy_hp"] = after
        context.enemy_hp = after
        result = BattleResult(
            damage_source=damage_source, attack_source=attack_source,
            raw_damage=raw_damage, final_damage=final_damage,
            **breakdown,
            crit_metadata=dict(crit_metadata or {}),
            enemy_hp_before=before, enemy_hp_after=after, lethal=after == 0,
        )
        if after:
            self.connection.execute(
                "UPDATE players SET enemy_hp = ? WHERE telegram_id = ?",
                (after, int(self.player["telegram_id"])),
            )
        else:
            transition = self.victory_handler(
                self.connection, self.player, boss, context
            )
            context.victory_transition = transition
            for name in (
                "reward_granted", "companion_healing", "stage_advanced",
                "boss_started", "boss_defeated", "next_battle_identity",
                "temporary_effects_cleared",
            ):
                setattr(result, name, transition.get(name, getattr(result, name)))
            self.player.update(transition.get("player", {}))
        return self.resolution.add(result)
