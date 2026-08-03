#!/usr/bin/env python3
"""Deterministic, standalone gear progression simulator.

The module deliberately has no dependency on api.py or the player database.  It
models economy and progression proposals before they are implemented in-game.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).with_name("gear_progression_config.json")


@dataclass(frozen=True)
class Item:
    slot: str
    rarity: str
    rarity_rank: int
    power: int
    damage: int
    hp: int
    hero_level: int
    stage: int
    chest_level: int
    roll: float
    sell_price: int


@dataclass
class PlayerState:
    day: int = 0
    hero_level: int = 1
    experience: int = 0
    max_stage: int = 0
    chest_level: int = 1
    chests: int = 0
    opened_chests: int = 0
    gold: int = 0
    equipment: dict[str, Item] = field(default_factory=dict)
    no_upgrade_streak: int = 0
    rarity_pity_counter: int = 0
    significant_upgrades: int = 0
    soft_wall_days: int = 0
    longest_wall_days: int = 0
    current_wall_days: int = 0


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    required = {"simulation", "hero", "combat", "chest", "item", "rarities", "rarity_weights", "pity", "profiles"}
    missing = required.difference(config)
    if missing:
        raise ValueError(f"Missing config sections: {sorted(missing)}")
    return config


class GearProgressionSimulator:
    def __init__(self, config: dict[str, Any], profile: str, *, pity: bool = True, seed: int | None = None):
        if profile not in config["profiles"]:
            raise ValueError(f"Unknown profile: {profile}")
        self.config = config
        self.profile_name = profile
        self.profile = config["profiles"][profile]
        self.pity_enabled = pity
        base_seed = int(config["simulation"]["seed"] if seed is None else seed)
        profile_offset = list(config["profiles"]).index(profile) * 100_003
        self.rng = random.Random(base_seed + profile_offset + (0 if pity else 50_000_003))
        self.state = PlayerState(
            chest_level=int(config["chest"]["start_level"]),
            chests=int(config["chest"]["starter_chests"]),
        )
        self.snapshots: dict[int, dict[str, Any]] = {}

    def hero_level_from_exp(self, experience: int) -> int:
        hero = self.config["hero"]
        maximum = int(hero["max_level"])
        target = float(hero["experience_to_max"])
        curve = float(hero["experience_curve_power"])
        ratio = max(0.0, min(1.0, experience / target))
        return max(1, min(maximum, 1 + int((ratio ** (1.0 / curve)) * (maximum - 1))))

    def chest_upgrade_cost(self, level: int) -> int:
        chest = self.config["chest"]
        return max(1, round(float(chest["upgrade_base_cost"]) * float(chest["upgrade_cost_growth"]) ** (level - 1)))

    def _rarity_weights(self, chest_level: int) -> list[float]:
        tables = sorted((int(level), weights) for level, weights in self.config["rarity_weights"].items())
        if chest_level <= tables[0][0]:
            return list(map(float, tables[0][1]))
        for (left_level, left), (right_level, right) in zip(tables, tables[1:]):
            if chest_level <= right_level:
                fraction = (chest_level - left_level) / (right_level - left_level)
                return [float(a) + (float(b) - float(a)) * fraction for a, b in zip(left, right)]
        return list(map(float, tables[-1][1]))

    def _roll_rarity(self, state: PlayerState, rng: random.Random, apply_pity: bool) -> dict[str, Any]:
        rarities = self.config["rarities"]
        rarity = rng.choices(rarities, weights=self._rarity_weights(state.chest_level), k=1)[0]
        pity = self.config["pity"]
        if apply_pity and state.rarity_pity_counter + 1 >= int(pity["guaranteed_rarity_every"]):
            minimum = int(pity["guaranteed_rarity_min_rank"])
            eligible = [entry for entry in rarities if int(entry["rank"]) >= minimum]
            rarity = rng.choices(eligible, weights=[1 / int(entry["rank"]) ** 2 for entry in eligible], k=1)[0]
            state.rarity_pity_counter = 0
        else:
            state.rarity_pity_counter += 1
        return rarity

    def generate_item(self, state: PlayerState | None = None, *, rng: random.Random | None = None, apply_pity: bool | None = None) -> Item:
        state = self.state if state is None else state
        rng = self.rng if rng is None else rng
        apply_pity = self.pity_enabled if apply_pity is None else apply_pity
        cfg = self.config
        pity = cfg["pity"]
        slots = cfg["item"]["slots"]
        force_upgrade = bool(
            apply_pity
            and state.equipment
            and state.no_upgrade_streak + 1 >= int(pity["guaranteed_upgrade_after"])
        )
        slot = rng.choice(list(slots))
        if force_upgrade:
            slot = min(state.equipment, key=lambda key: state.equipment[key].power)
        rarity = self._roll_rarity(state, rng, apply_pity)
        hero = cfg["hero"]
        effective_stage = min(
            max(1, state.max_stage),
            max(1, state.hero_level * int(hero["equipment_stage_cap_per_level"])),
        )
        chest = cfg["chest"]
        spread = float(chest["random_bonus_base"]) + effective_stage * float(chest["random_bonus_stage_scale"])
        roll = rng.uniform(max(0.5, 1.0 - spread), 1.0 + spread)
        item_cfg = cfg["item"]
        base_power = (
            effective_stage * float(item_cfg["stage_power_scale"])
            + state.hero_level * float(item_cfg["hero_level_power_scale"])
            + state.chest_level * float(item_cfg["chest_level_power_scale"])
        )
        power = max(1, round(base_power * (1 + (state.chest_level - 1) * float(chest["quality_power_per_level"])) * float(rarity["power"]) * roll))
        if force_upgrade:
            current = state.equipment[slot].power
            total_power = sum(entry.power for entry in state.equipment.values())
            significant_gain = math.ceil(
                max(1, total_power)
                * float(self.config["simulation"]["significant_upgrade_ratio"])
            )
            power = max(
                power,
                math.ceil(current * (1 + float(pity["forced_upgrade_power_ratio"]))),
                current + significant_gain,
            )
        focus = slots[slot]
        damage = round(power * float(item_cfg["damage_per_power_attack"])) if focus == "attack" else round(power * float(item_cfg["damage_per_power_mixed"])) if focus == "mixed" else 0
        hp = round(power * float(item_cfg["hp_per_power_defense"])) if focus == "defense" else round(power * float(item_cfg["hp_per_power_mixed"])) if focus == "mixed" else 0
        sell_price = max(1, round(power * float(rarity["sell"]) * (float(chest["sell_base_multiplier"]) + state.chest_level * float(chest["sell_chest_level_multiplier"]))))
        return Item(slot, str(rarity["key"]), int(rarity["rank"]), power, damage, hp, state.hero_level, effective_stage, state.chest_level, round(roll, 5), sell_price)

    def _is_significant(self, item: Item, old: Item | None) -> bool:
        old_total = sum(entry.power for entry in self.state.equipment.values())
        gain = item.power - (old.power if old else 0)
        threshold = max(1, math.ceil(max(1, old_total) * float(self.config["simulation"]["significant_upgrade_ratio"])))
        return gain >= threshold

    def open_chest(self) -> bool:
        if self.state.chests <= 0:
            return False
        state = self.state
        state.chests -= 1
        state.opened_chests += 1
        hero = self.config["hero"]
        state.experience += max(1, round(float(hero["experience_per_chest_base"]) + (state.chest_level - 1) * float(hero["experience_per_chest_level"])))
        state.hero_level = self.hero_level_from_exp(state.experience)
        item = self.generate_item()
        old = state.equipment.get(item.slot)
        upgraded = old is None or item.power > old.power
        significant = upgraded and self._is_significant(item, old)
        if upgraded:
            if old:
                state.gold += old.sell_price
            state.equipment[item.slot] = item
            state.no_upgrade_streak = 0
            state.significant_upgrades += int(significant)
        else:
            state.gold += item.sell_price
            state.no_upgrade_streak += 1
        self._upgrade_chest_while_possible()
        return significant

    def _upgrade_chest_while_possible(self) -> None:
        state = self.state
        maximum = int(self.config["chest"]["max_level"])
        while state.chest_level < maximum:
            cost = self.chest_upgrade_cost(state.chest_level)
            if state.gold < cost:
                break
            state.gold -= cost
            state.chest_level += 1

    def combat_stats(self, day: int | None = None) -> dict[str, float]:
        state = self.state
        day = state.day if day is None else day
        hero = self.config["hero"]
        profile = self.profile
        simulation = self.config["simulation"]
        growth_days = max(1, int(simulation["external_system_growth_days"]))
        progress = min(1.0, max(0.0, day / growth_days)) ** float(
            simulation["external_system_growth_curve"]
        )
        skill_bonus = float(profile["skill_damage_bonus_day_365"]) * progress
        companion_damage = float(profile["companion_damage_bonus_day_365"]) * progress
        companion_hp = float(profile["companion_hp_bonus_day_365"]) * progress
        base_damage = float(hero["base_damage"]) + (state.hero_level - 1) * float(hero["damage_per_level"])
        base_hp = float(hero["base_hp"]) + (state.hero_level - 1) * float(hero["hp_per_level"])
        gear_damage = sum(item.damage for item in state.equipment.values())
        gear_hp = sum(item.hp for item in state.equipment.values())
        total_damage = (base_damage + gear_damage) * (1 + skill_bonus) * (1 + companion_damage)
        total_hp = (base_hp + gear_hp) * (1 + companion_hp)
        equipment_power = sum(item.power for item in state.equipment.values())
        base_power = base_damage + base_hp / 4
        share = equipment_power / (equipment_power + base_power) if equipment_power else 0.0
        return {"base_damage": base_damage, "gear_damage": gear_damage, "damage": total_damage, "hp": total_hp, "equipment_power": equipment_power, "equipment_share": share}

    def enemy_stats(self, stage: int, boss: bool = False) -> tuple[int, int]:
        combat = self.config["combat"]
        hp = float(combat["base_enemy_hp"]) * (1 + float(combat["enemy_hp_stage_growth"]) * (stage - 1)) ** float(combat["enemy_hp_power"])
        damage = float(combat["base_enemy_damage"]) * (1 + float(combat["enemy_damage_stage_growth"]) * (stage - 1)) ** float(combat["enemy_damage_power"])
        if boss:
            milestone = stage % 10 == 0
            hp *= float(combat["milestone_boss_hp_multiplier"] if milestone else combat["boss_hp_multiplier"])
            damage *= float(combat["milestone_boss_damage_multiplier"] if milestone else combat["boss_damage_multiplier"])
        return max(1, round(hp)), max(1, round(damage))

    def kill_time(self, stage: int, boss: bool = False) -> float:
        hp, _ = self.enemy_stats(stage, boss)
        dps = self.combat_stats()["damage"] * float(self.config["combat"]["hero_attacks_per_second"])
        return hp / max(1.0, dps)

    def _can_beat_boss(self, stage: int) -> bool:
        combat = self.config["combat"]
        seconds = self.kill_time(stage, True)
        _, damage = self.enemy_stats(stage, True)
        attacks_received = max(0, math.floor(seconds / float(combat["enemy_attack_interval_seconds"])))
        survivable = self.combat_stats()["hp"] / damage
        return seconds <= float(combat["maximum_boss_kill_seconds"]) and survivable >= max(int(combat["minimum_boss_hits_survived"]), attacks_received)

    def _progress_combat(self, seconds_budget: float) -> int:
        combat = self.config["combat"]
        gained = 0
        while self.state.max_stage < int(self.config["simulation"]["max_stage"]):
            stage = self.state.max_stage + 1
            if not self._can_beat_boss(stage):
                break
            normal_time = self.kill_time(stage, False)
            boss_time = self.kill_time(stage, True)
            stage_time = int(combat["enemies_per_stage"]) * (normal_time + float(combat["normal_enemy_transition_seconds"])) + boss_time + float(combat["stage_transition_seconds"])
            if stage_time > seconds_budget:
                break
            seconds_budget -= stage_time
            normal_chests = int(combat["enemies_per_stage"]) * int(combat["normal_chests_per_kill"]) + int(combat["last_normal_chest_bonus"])
            boss_chests = int(combat["milestone_boss_chests"] if stage % 10 == 0 else combat["boss_chests"])
            gained += normal_chests + boss_chests
            self.state.max_stage = stage
        self.state.chests += gained
        return gained

    def _open_all(self) -> None:
        while self.state.chests > 0:
            self.open_chest()

    def run_day(self) -> None:
        state = self.state
        state.day += 1
        before = state.max_stage
        p = self.profile
        state.chests += int(p["daily_quest_chests"]) + int(p["offline_chests"]) + int(p["pass_chests"]) + int(p["paid_chests_daily_limit"])
        self._open_all()
        # Two combat/opening passes let a chest batch break a wall, while the
        # finite active-time budget prevents an infinite progression cascade.
        budget = float(p["active_combat_seconds"])
        first_pass_ratio = float(
            self.config["simulation"]["daily_combat_first_pass_ratio"]
        )
        self._progress_combat(budget * first_pass_ratio)
        self._open_all()
        self._progress_combat(budget * (1 - first_pass_ratio))
        self._open_all()
        if state.max_stage == before and state.max_stage < int(self.config["simulation"]["max_stage"]):
            state.soft_wall_days += 1
            state.current_wall_days += 1
            state.longest_wall_days = max(state.longest_wall_days, state.current_wall_days)
        else:
            state.current_wall_days = 0

    def _upgrade_probability(self) -> float:
        samples = int(self.config["simulation"]["monte_carlo_samples"])
        if not self.state.equipment:
            return 1.0
        trial_state = copy.deepcopy(self.state)
        trial_rng = random.Random(int(self.config["simulation"]["seed"]) + self.state.day * 7919 + 17)
        successes = 0
        for _ in range(samples):
            trial_state.rarity_pity_counter = 0
            trial_state.no_upgrade_streak = 0
            item = self.generate_item(trial_state, rng=trial_rng, apply_pity=False)
            old = self.state.equipment.get(item.slot)
            successes += int((old is None or item.power > old.power) and self._is_significant(item, old))
        return successes / samples

    def snapshot(self) -> dict[str, Any]:
        state = self.state
        probability = self._upgrade_probability()
        stats = self.combat_stats()
        stage = max(1, state.max_stage + 1)
        average_rarity = sum(item.rarity_rank for item in state.equipment.values()) / max(1, len(state.equipment))
        natural_expected = 1 / probability if probability else math.inf
        if self.pity_enabled:
            pity_remaining = max(
                1,
                int(self.config["pity"]["guaranteed_upgrade_after"])
                - state.no_upgrade_streak,
            )
            expected = min(natural_expected, float(pity_remaining))
            def no_upgrade_probability(openings: int) -> float:
                return 0.0 if openings >= pity_remaining else (1 - probability) ** openings
        else:
            expected = natural_expected
            def no_upgrade_probability(openings: int) -> float:
                return (1 - probability) ** openings
        return {
            "day": state.day,
            "hero_level": state.hero_level,
            "max_stage": state.max_stage,
            "chest_level": state.chest_level,
            "opened_chests": state.opened_chests,
            "average_equipped_rarity": round(average_rarity, 2),
            "total_damage": round(stats["damage"]),
            "total_hp": round(stats["hp"]),
            "equipment_power_share": round(stats["equipment_share"], 4),
            "expected_chests_per_significant_upgrade": round(expected, 2) if math.isfinite(expected) else None,
            "no_upgrade_probability_50": round(no_upgrade_probability(50), 6),
            "no_upgrade_probability_100": round(no_upgrade_probability(100), 6),
            "no_upgrade_probability_500": round(no_upgrade_probability(500), 6),
            "normal_enemy_kill_seconds": round(self.kill_time(stage, False), 2),
            "boss_kill_seconds": round(self.kill_time(stage, True), 2),
            "soft_wall_days_total": state.soft_wall_days,
            "longest_soft_wall_days": state.longest_wall_days,
            "gold": state.gold,
            "significant_upgrades": state.significant_upgrades,
        }

    def run(self, days: int | None = None) -> dict[str, Any]:
        days = int(self.config["simulation"]["days"] if days is None else days)
        checkpoints = set(int(day) for day in self.config["simulation"]["checkpoints"] if int(day) <= days)
        for _ in range(days):
            self.run_day()
            if self.state.day in checkpoints:
                self.snapshots[self.state.day] = self.snapshot()
        return {
            "profile": self.profile_name,
            "pity_enabled": self.pity_enabled,
            "snapshots": [self.snapshots[day] for day in sorted(self.snapshots)],
            "final_state": asdict(self.state),
        }


def simulate_all(config: dict[str, Any], *, days: int | None = None, pity: bool = True) -> dict[str, Any]:
    return {profile: GearProgressionSimulator(config, profile, pity=pity).run(days) for profile in config["profiles"]}


def pity_comparison(config: dict[str, Any], *, days: int | None = None) -> dict[str, Any]:
    with_pity = simulate_all(config, days=days, pity=True)
    without_pity = simulate_all(config, days=days, pity=False)
    comparison = {}
    for profile in config["profiles"]:
        yes = with_pity[profile]["snapshots"][-1]
        no = without_pity[profile]["snapshots"][-1]
        comparison[profile] = {
            "with_pity": yes,
            "without_pity": no,
            "stage_delta": yes["max_stage"] - no["max_stage"],
            "damage_delta_percent": round((yes["total_damage"] / max(1, no["total_damage"]) - 1) * 100, 2),
            "longest_wall_delta_days": yes["longest_soft_wall_days"] - no["longest_soft_wall_days"],
        }
    return comparison


def print_report(results: dict[str, Any]) -> None:
    columns = ("day", "hero_level", "max_stage", "chest_level", "opened_chests", "average_equipped_rarity", "total_damage", "total_hp", "equipment_power_share", "expected_chests_per_significant_upgrade", "normal_enemy_kill_seconds", "boss_kill_seconds")
    for profile, result in results.items():
        print(f"\n[{profile}] pity={result['pity_enabled']}")
        print(" | ".join(columns))
        for row in result["snapshots"]:
            print(" | ".join(str(row[column]) for column in columns))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--days", type=int)
    parser.add_argument("--profile", choices=("all", "f2p", "low_spender", "mid_spender", "whale"), default="all")
    parser.add_argument("--no-pity", action="store_true")
    parser.add_argument("--compare-pity", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.compare_pity:
        result = pity_comparison(config, days=args.days)
    elif args.profile == "all":
        result = simulate_all(config, days=args.days, pity=not args.no_pity)
    else:
        result = {args.profile: GearProgressionSimulator(config, args.profile, pity=not args.no_pity).run(args.days)}
    if args.as_json or args.compare_pity:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
