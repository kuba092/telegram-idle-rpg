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
import statistics
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
    required = {
        "simulation", "hero", "combat", "chest", "item", "rarities",
        "rarity_weights", "pity", "skills", "companions", "builds",
        "build_simulation", "profiles",
    }
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

    def system_level(self, system: str, day: int | None = None) -> int:
        """Return a profile/day driven skill or companion level."""
        if system not in {"skill", "companion"}:
            raise ValueError(f"Unknown progression system: {system}")
        day = self.state.day if day is None else day
        simulation = self.config["simulation"]
        growth_days = max(1, int(simulation["external_system_growth_days"]))
        progress = min(1.0, max(0.0, (day - 1) / max(1, growth_days - 1))) ** float(
            simulation["external_system_growth_curve"]
        )
        start = int(self.profile[f"{system}_level_day_1"])
        end = int(self.profile[f"{system}_level_day_365"])
        maximum = int(self.config[f"{system}s"]["max_level"])
        return max(1, min(maximum, round(start + (end - start) * progress)))

    def active_build(self, build_name: str, day: int | None = None) -> dict[str, Any]:
        if build_name not in self.config["builds"]:
            raise ValueError(f"Unknown build: {build_name}")
        definition = self.config["builds"][build_name]
        hero_level = self.state.hero_level
        skill_slots = sum(hero_level >= level for level in self.config["skills"]["slot_unlock_levels"])
        companion_slots = (
            sum(hero_level >= level for level in self.config["companions"]["slot_unlock_levels"])
            if hero_level >= int(self.config["companions"]["system_unlock_level"])
            else 0
        )
        skill_level = min(
            self.system_level("skill", day),
            int(definition.get("skill_level_cap", self.config["skills"]["max_level"])),
        )
        companion_level = min(
            self.system_level("companion", day),
            int(definition.get("companion_level_cap", self.config["companions"]["max_level"])),
        )
        return {
            "name": build_name,
            "skills": [
                {"id": skill_id, "level": skill_level}
                for skill_id in definition["skills"][:skill_slots]
            ],
            "companions": [
                {"id": companion_id, "level": companion_level}
                for companion_id in definition["companions"][:companion_slots]
            ],
            "skill_slots": skill_slots,
            "companion_slots": companion_slots,
        }

    def combat_stats(self, day: int | None = None, build_name: str | None = None) -> dict[str, float]:
        state = self.state
        day = state.day if day is None else day
        hero = self.config["hero"]
        build_name = build_name or str(self.config["build_simulation"]["default_progression_build"])
        build = self.active_build(build_name, day)
        companion_levels = {entry["id"]: entry["level"] for entry in build["companions"]}
        companion_cfg = self.config["companions"]
        forest_bonus = companion_levels.get("forest_sprite", 0) * float(companion_cfg["forest_sprite"]["per_level"])
        slime_bonus = companion_levels.get("baby_slime", 0) * float(companion_cfg["baby_slime"]["per_level"])
        base_damage = float(hero["base_damage"]) + (state.hero_level - 1) * float(hero["damage_per_level"])
        base_hp = float(hero["base_hp"]) + (state.hero_level - 1) * float(hero["hp_per_level"])
        gear_damage = sum(item.damage for item in state.equipment.values())
        gear_hp = sum(item.hp for item in state.equipment.values())
        total_damage = (base_damage + gear_damage) * (1 + forest_bonus)
        total_hp = (base_hp + gear_hp) * (1 + slime_bonus)
        equipment_power = sum(item.power for item in state.equipment.values())
        base_power = base_damage + base_hp / 4
        share = equipment_power / (equipment_power + base_power) if equipment_power else 0.0
        return {
            "base_damage": base_damage, "gear_damage": gear_damage,
            "raw_damage": base_damage + gear_damage, "damage": total_damage,
            "base_hp": base_hp, "gear_hp": gear_hp,
            "raw_hp": base_hp + gear_hp, "hp": total_hp,
            "equipment_power": equipment_power, "equipment_share": share,
        }

    def enemy_stats(self, stage: int, boss: bool = False) -> tuple[int, int]:
        combat = self.config["combat"]
        hp = float(combat["base_enemy_hp"]) * (1 + float(combat["enemy_hp_stage_growth"]) * (stage - 1)) ** float(combat["enemy_hp_power"])
        damage = float(combat["base_enemy_damage"]) * (1 + float(combat["enemy_damage_stage_growth"]) * (stage - 1)) ** float(combat["enemy_damage_power"])
        late_stages = max(0, stage - int(combat["late_game_scaling_start_stage"]))
        hp *= (1 + float(combat["late_game_enemy_hp_growth_per_stage"])) ** late_stages
        damage *= (1 + float(combat["late_game_enemy_damage_growth_per_stage"])) ** late_stages
        if boss:
            milestone = stage % 10 == 0
            hp *= float(combat["milestone_boss_hp_multiplier"] if milestone else combat["boss_hp_multiplier"])
            damage *= float(combat["milestone_boss_damage_multiplier"] if milestone else combat["boss_damage_multiplier"])
        return max(1, round(hp)), max(1, round(damage))

    def _critical_multiplier(self, critical: bool, expected: bool, chance: float, crit_multiplier: float, rng: random.Random) -> float:
        if expected:
            return 1.0 + chance * (crit_multiplier - 1.0)
        return crit_multiplier if critical or rng.random() < chance else 1.0

    def simulate_battle(
        self,
        stage: int,
        build_name: str,
        *,
        boss: bool = False,
        day: int | None = None,
        rng: random.Random | None = None,
        expected_crits: bool = True,
        excluded_companion: str | None = None,
        initial_hp: float | None = None,
        target_duration: float | None = None,
    ) -> dict[str, Any]:
        """Simulate one isolated fight; temporary effects reset on every call."""
        day = self.state.day if day is None else day
        rng = self.rng if rng is None else rng
        cfg = self.config
        combat = cfg["combat"]
        skills_cfg = cfg["skills"]
        companions_cfg = cfg["companions"]
        build = self.active_build(build_name, day)
        skill_levels = {entry["id"]: entry["level"] for entry in build["skills"]}
        companion_levels = {entry["id"]: entry["level"] for entry in build["companions"]}
        if excluded_companion:
            companion_levels.pop(excluded_companion, None)
        stats = self.combat_stats(day, build_name)
        forest_bonus = companion_levels.get("forest_sprite", 0) * float(companions_cfg["forest_sprite"]["per_level"])
        forest_multiplier = 1 + forest_bonus
        poison_forest_multiplier = 1 + forest_bonus * float(companions_cfg["forest_sprite"]["poison_cloud_effectiveness"])
        beetle_extra = stats["raw_damage"] * companion_levels.get("spore_beetle", 0) * float(companions_cfg["spore_beetle"]["per_level"])
        beetle_attack_speed = min(
            float(companions_cfg["spore_beetle"]["maximum_attack_speed_bonus"]),
            companion_levels.get("spore_beetle", 0) * float(companions_cfg["spore_beetle"]["attack_speed_per_level"]),
        )
        owl_reduction = min(
            float(companions_cfg["mushroom_owl"]["maximum_reduction"]),
            companion_levels.get("mushroom_owl", 0) * float(companions_cfg["mushroom_owl"]["per_level"]),
        )
        cooldown_multiplier = 1 - owl_reduction
        crit_chance_percent = float(cfg["build_simulation"]["base_crit_chance_percent"]) + companion_levels.get("thorn_wolf", 0) * float(companions_cfg["thorn_wolf"]["percentage_points_per_level"])
        crit_chance = min(float(companions_cfg["thorn_wolf"]["maximum_total_chance"]), crit_chance_percent) / 100
        crit_damage = (
            float(cfg["build_simulation"]["base_crit_damage_percent"])
            + (self.state.hero_level // 10) * float(cfg["build_simulation"]["crit_damage_percent_per_10_hero_levels"])
            + companion_levels.get("thorn_wolf", 0) * float(companions_cfg["thorn_wolf"]["critical_damage_percent_per_level"])
        ) / 100
        enemy_hp, enemy_damage = self.enemy_stats(stage, boss)
        if target_duration is not None:
            enemy_hp = math.inf
            enemy_damage = 0
        max_hp = stats["raw_hp"] * (
            1 + companion_levels.get("baby_slime", 0) * float(companions_cfg["baby_slime"]["per_level"])
        )
        hero_hp = max_hp if initial_hp is None else min(max_hp, max(0.0, float(initial_hp)))
        hero_hp_at_start = hero_hp
        shield = 0.0
        time_now = 0.0
        attack_interval = 1 / (float(combat["hero_attacks_per_second"]) * (1 + beetle_attack_speed))
        next_attack = 0.0
        next_enemy_attack = math.inf if target_duration is not None else float(combat["enemy_attack_interval_seconds"])
        next_skill = {skill_id: 0.0 for skill_id in skill_levels if skill_id != "mushroom_shield"}
        shield_ready_at = 0.0
        # tick time, cloud id, damage multiplier; each cloud tracks completion.
        poison_ticks: list[tuple[float, int, float]] = []
        poison_cloud_landed: dict[int, int] = {}
        poison_completed_stacks = 0
        poison_ticks_landed = 0
        damage_by_source = {"normal_attack": 0.0, "spore_strike": 0.0, "poison_cloud": 0.0}
        uses = {skill_id: 0 for skill_id in skill_levels}
        skill_cast_multipliers = {skill_id: [] for skill_id in skill_levels}
        poison_cloud_cast_multipliers: list[float] = []
        shield_generated = 0.0
        shield_cast_times: list[float] = []
        shield_absorbed = 0.0
        shield_damage_reduced = 0.0
        hero_damage_taken = 0.0
        shield_breaks = 0
        mitigation_until = -math.inf
        enemy_attacks = 0
        max_seconds = float(target_duration if target_duration is not None else cfg["simulation"]["combat_max_seconds"])
        epsilon = float(cfg["simulation"]["combat_time_step"])

        def apply_damage(source: str, raw: float, can_crit: bool = True, critical_bonus: float = 0.0) -> None:
            nonlocal enemy_hp
            critical_multiplier = crit_damage + critical_bonus
            multiplier = self._critical_multiplier(False, expected_crits, crit_chance, critical_multiplier, rng) if can_crit else 1.0
            dealt = max(0.0, raw * multiplier)
            enemy_hp -= dealt
            damage_by_source[source] += dealt

        while enemy_hp > 0 and hero_hp > 0:
            candidates = [next_attack, next_enemy_attack]
            candidates.extend(next_skill.values())
            candidates.extend(tick[0] for tick in poison_ticks)
            next_event = min(value for value in candidates if value >= time_now - epsilon)
            if next_event > max_seconds + epsilon:
                time_now = max_seconds
                break
            time_now = next_event

            if next_attack <= time_now + epsilon:
                # Beetle is part of the normal attack hit, so the whole hit can crit.
                apply_damage("normal_attack", stats["raw_damage"] * forest_multiplier + beetle_extra)
                next_attack += attack_interval
                if enemy_hp <= 0:
                    break

            for skill_id in list(next_skill):
                if next_skill[skill_id] > time_now + epsilon:
                    continue
                level = skill_levels[skill_id]
                power = 1 + (level - 1) * float(skills_cfg["power_per_level"])
                skill_cfg = skills_cfg[skill_id]
                repeat_stacks = min(uses[skill_id], int(companions_cfg["mushroom_owl"]["maximum_repeat_stacks"])) if companion_levels.get("mushroom_owl", 0) else 0
                repeat_multiplier = 1 + repeat_stacks * float(companions_cfg["mushroom_owl"]["repeat_use_bonus"])
                uses[skill_id] += 1
                skill_cast_multipliers[skill_id].append(repeat_multiplier)
                if skill_id == "spore_strike":
                    wolf_bonus = (
                        companion_levels.get("thorn_wolf", 0)
                        * float(companions_cfg["thorn_wolf"]["critical_damage_percent_per_level"]) / 100
                        * float(companions_cfg["thorn_wolf"]["spore_strike_critical_damage_synergy"])
                    )
                    apply_damage(skill_id, stats["raw_damage"] * float(skill_cfg["damage_multiplier"]) * power * forest_multiplier * repeat_multiplier, critical_bonus=wolf_bonus)
                elif skill_id == "poison_cloud":
                    cloud_id = uses[skill_id]
                    poison_cloud_landed[cloud_id] = 0
                    cloud_multiplier = (
                        repeat_multiplier
                        * (1 + poison_completed_stacks * float(skill_cfg["completed_cloud_bonus"]))
                    )
                    poison_cloud_cast_multipliers.append(cloud_multiplier)
                    poison_ticks.extend(
                        (time_now + float(skill_cfg["tick_interval_seconds"]) * tick, cloud_id, cloud_multiplier)
                        for tick in range(1, int(skill_cfg["ticks"]) + 1)
                    )
                next_skill[skill_id] += float(skill_cfg["cooldown_seconds"]) * cooldown_multiplier
                if enemy_hp <= 0:
                    break
            if enemy_hp <= 0:
                break

            due_ticks = [tick for tick in poison_ticks if tick[0] <= time_now + epsilon]
            if due_ticks:
                level = skill_levels.get("poison_cloud", 1)
                power = 1 + (level - 1) * float(skills_cfg["power_per_level"])
                tick_cfg = skills_cfg["poison_cloud"]
                for _, cloud_id, cloud_multiplier in due_ticks:
                    apply_damage("poison_cloud", stats["raw_damage"] * float(tick_cfg["tick_damage_multiplier"]) * power * poison_forest_multiplier * cloud_multiplier)
                    poison_ticks_landed += 1
                    poison_cloud_landed[cloud_id] += 1
                    if poison_cloud_landed[cloud_id] == int(tick_cfg["ticks"]):
                        poison_completed_stacks = min(
                            int(tick_cfg["maximum_completed_cloud_stacks"]), poison_completed_stacks + 1
                        )
                poison_ticks = [tick for tick in poison_ticks if tick[0] > time_now + epsilon]
                if enemy_hp <= 0:
                    break

            if next_enemy_attack <= time_now + epsilon:
                if "mushroom_shield" in skill_levels and hero_hp / max_hp <= float(skills_cfg["mushroom_shield"]["auto_hp_ratio"]) and time_now >= shield_ready_at - epsilon:
                    level = skill_levels["mushroom_shield"]
                    power = 1 + (level - 1) * float(skills_cfg["power_per_level"])
                    repeat_stacks = min(uses["mushroom_shield"], int(companions_cfg["mushroom_owl"]["maximum_repeat_stacks"])) if companion_levels.get("mushroom_owl", 0) else 0
                    repeat_multiplier = 1 + repeat_stacks * float(companions_cfg["mushroom_owl"]["repeat_use_bonus"])
                    shield = max_hp * float(skills_cfg["mushroom_shield"]["max_hp_ratio"]) * power * repeat_multiplier
                    shield_generated += shield
                    shield_cast_times.append(time_now)
                    uses["mushroom_shield"] += 1
                    skill_cast_multipliers["mushroom_shield"].append(repeat_multiplier)
                    shield_ready_at = time_now + float(skills_cfg["mushroom_shield"]["cooldown_seconds"]) * cooldown_multiplier
                shield_before = shield
                absorbed = min(shield, enemy_damage)
                shield -= absorbed
                shield_absorbed += absorbed
                incoming = max(0.0, enemy_damage - absorbed)
                if shield_before > 0 and shield <= epsilon:
                    shield_breaks += 1
                    mitigation_until = time_now + float(skills_cfg["mushroom_shield"]["break_reduction_seconds"])
                if time_now <= mitigation_until + epsilon:
                    reduction = incoming * float(skills_cfg["mushroom_shield"]["break_damage_reduction"])
                    incoming -= reduction
                    shield_damage_reduced += reduction
                hero_hp -= incoming
                hero_damage_taken += incoming
                enemy_attacks += 1
                next_enemy_attack += float(combat["enemy_attack_interval_seconds"])

        won = enemy_hp <= 0
        duration = max(epsilon, float(target_duration) if target_duration is not None else time_now)
        entling_ratio = companion_levels.get("ancient_entling", 0) * float(companions_cfg["ancient_entling"]["normal_ratio_per_level"])
        if boss:
            entling_ratio *= float(companions_cfg["ancient_entling"]["boss_multiplier"])
        post_victory_healing = min(max_hp - max(0.0, hero_hp), max_hp * entling_ratio) if won else 0.0
        skill_dps = {skill: damage / duration for skill, damage in damage_by_source.items() if skill != "normal_attack"}
        return {
            "build": build_name, "stage": stage, "boss": boss,
            "won": won, "survived": hero_hp > 0, "duration": duration,
            "hero_hp_at_start": hero_hp_at_start,
            "normal_attack_damage": stats["raw_damage"] * forest_multiplier + beetle_extra,
            "attack_interval": attack_interval, "attack_speed_bonus": beetle_attack_speed,
            "crit_chance": crit_chance, "crit_damage_multiplier": crit_damage,
            "expected_crit_multiplier": 1 + crit_chance * (crit_damage - 1),
            "expected_critical_attack_damage": (stats["raw_damage"] * forest_multiplier + beetle_extra) * crit_damage,
            "damage_by_source": damage_by_source, "skill_dps": skill_dps,
            "total_dps": sum(damage_by_source.values()) / duration,
            "max_hp": max_hp, "effective_hp": max_hp + shield_generated,
            "hero_hp_after_battle": max(0.0, hero_hp), "shield_generated": shield_generated,
            "shield_absorbed": shield_absorbed, "shield_damage_reduced": shield_damage_reduced,
            "shield_cast_times": shield_cast_times,
            "hero_damage_taken": hero_damage_taken,
            "shield_breaks": shield_breaks, "post_victory_healing": post_victory_healing,
            "skill_uses": uses,
            "skill_cast_multipliers": skill_cast_multipliers,
            "poison_cloud_cast_multipliers": poison_cloud_cast_multipliers,
            "skill_frequency_per_second": {
                skill_id: count / duration for skill_id, count in uses.items()
            },
            "skill_intervals": {
                skill_id: float(skills_cfg[skill_id]["cooldown_seconds"]) * cooldown_multiplier
                for skill_id in skill_levels if "cooldown_seconds" in skills_cfg[skill_id]
            },
            "poison_ticks_landed": poison_ticks_landed,
            "poison_completed_stacks": poison_completed_stacks,
            "enemy_attacks": enemy_attacks,
            "active_skills": skill_levels, "active_companions": companion_levels,
        }

    def damage_over_duration(self, build_name: str, duration: float, *, day: int | None = None) -> dict[str, float]:
        """Expected target-dummy damage over a fixed window."""
        if duration <= 0:
            raise ValueError("duration must be positive")
        battle = self.simulate_battle(
            1, build_name, day=day, expected_crits=True, target_duration=float(duration)
        )
        return {
            "duration": float(duration), "damage": sum(battle["damage_by_source"].values()),
            "dps": battle["total_dps"], **battle["damage_by_source"],
        }

    def simulate_full_stage(self, stage: int, build_name: str, *, day: int | None = None) -> dict[str, Any]:
        """Run ten enemies then a boss while carrying HP; cooldowns reset per fight."""
        day = self.state.day if day is None else day
        current_hp: float | None = None
        deaths = 0
        total_time = 0.0
        total_healing = 0.0
        total_shield_absorbed = 0.0
        total_shield_damage_reduced = 0.0
        total_hero_damage_taken = 0.0
        hp_before_boss = 0.0
        battles = []
        battle_start_hp = []
        sequence = [False] * int(self.config["combat"]["enemies_per_stage"]) + [True]
        for index, boss in enumerate(sequence):
            if boss:
                hp_before_boss = 0.0 if current_hp is None else current_hp
            battle = self.simulate_battle(
                stage, build_name, boss=boss, day=day, initial_hp=current_hp
            )
            battles.append(battle)
            battle_start_hp.append(battle["hero_hp_at_start"])
            total_time += battle["duration"]
            total_shield_absorbed += battle["shield_absorbed"]
            total_shield_damage_reduced += battle["shield_damage_reduced"]
            total_hero_damage_taken += battle["hero_damage_taken"]
            if not battle["won"]:
                deaths += 1
                current_hp = 0.0
                break
            total_time += float(
                self.config["combat"]["stage_transition_seconds"]
                if boss else self.config["combat"]["normal_enemy_transition_seconds"]
            )
            total_healing += battle["post_victory_healing"]
            current_hp = min(
                battle["max_hp"],
                battle["hero_hp_after_battle"] + battle["post_victory_healing"],
            )
        max_hp = battles[0]["max_hp"] if battles else 1.0
        return {
            "build": build_name, "stage": stage, "completed": len(battles) == len(sequence) and deaths == 0,
            "hp_before_boss": hp_before_boss, "hp_before_boss_percent": hp_before_boss / max_hp * 100,
            "deaths": deaths, "total_time": total_time,
            "remaining_hp_after_boss": current_hp or 0.0,
            "remaining_hp_after_boss_percent": (current_hp or 0.0) / max_hp * 100,
            "entling_healing": total_healing, "shield_absorbed": total_shield_absorbed,
            "shield_damage_reduced": total_shield_damage_reduced,
            "hero_damage_taken": total_hero_damage_taken,
            "cooldowns_reset_between_fights": True, "battles_completed": len(battles),
            "battle_start_hp": battle_start_hp,
        }

    def monte_carlo_battle(self, stage: int, build_name: str, *, boss: bool, day: int | None = None, trials: int | None = None) -> dict[str, Any]:
        trials = int(trials or self.config["simulation"]["combat_monte_carlo_trials"])
        seed = int(self.config["simulation"]["seed"]) + stage * 104729 + self.state.day * 1009 + list(self.config["builds"]).index(build_name) * 9176 + int(boss)
        results = [
            self.simulate_battle(stage, build_name, boss=boss, day=day, rng=random.Random(seed + trial), expected_crits=False)
            for trial in range(trials)
        ]
        return {
            "win_probability": sum(result["won"] for result in results) / trials,
            "survival_probability": sum(result["survived"] for result in results) / trials,
            "mean_duration": statistics.fmean(result["duration"] for result in results),
            "mean_dps": statistics.fmean(result["total_dps"] for result in results),
        }

    def monte_carlo_random_builds(self, stage: int, *, day: int | None = None, trials: int | None = None) -> dict[str, Any]:
        """Sample random valid skill/companion combinations with deterministic RNG."""
        day = self.state.day if day is None else day
        trials = int(trials or self.config["simulation"]["combat_monte_carlo_trials"])
        seed = int(self.config["simulation"]["seed"]) + stage * 65537 + day * 8191 + list(self.config["profiles"]).index(self.profile_name) * 131071
        rng = random.Random(seed)
        skill_ids = [key for key in self.config["skills"] if key not in {"slot_unlock_levels", "max_level", "power_per_level"}]
        companion_ids = [key for key in self.config["companions"] if isinstance(self.config["companions"][key], dict)]
        skill_slots = sum(self.state.hero_level >= level for level in self.config["skills"]["slot_unlock_levels"])
        companion_slots = sum(self.state.hero_level >= level for level in self.config["companions"]["slot_unlock_levels"])
        temporary_name = "__monte_carlo_random_build__"
        results = []
        best = None
        try:
            for trial in range(trials):
                skills = rng.sample(skill_ids, k=min(skill_slots, len(skill_ids)))
                companions = rng.sample(companion_ids, k=min(companion_slots, len(companion_ids)))
                self.config["builds"][temporary_name] = {"skills": skills, "companions": companions}
                result = self.simulate_battle(
                    stage, temporary_name, boss=True, day=day,
                    rng=random.Random(seed + trial + 1), expected_crits=False,
                )
                record = {
                    "skills": skills, "companions": companions,
                    "won": result["won"], "dps": result["total_dps"],
                    "duration": result["duration"],
                }
                results.append(record)
                if best is None or (record["won"], record["dps"]) > (best["won"], best["dps"]):
                    best = record
        finally:
            self.config["builds"].pop(temporary_name, None)
        return {
            "trials": trials,
            "win_probability": sum(record["won"] for record in results) / trials,
            "mean_dps": statistics.fmean(record["dps"] for record in results),
            "best_sample": best,
        }

    def kill_time(self, stage: int, boss: bool = False, build_name: str | None = None) -> float:
        build_name = build_name or str(self.config["build_simulation"]["default_progression_build"])
        battle = self.simulate_battle(stage, build_name, boss=boss)
        return battle["duration"] if battle["won"] else float(self.config["simulation"]["combat_max_seconds"])

    def _can_beat_boss(self, stage: int) -> bool:
        combat = self.config["combat"]
        battle = self.simulate_battle(
            stage,
            str(self.config["build_simulation"]["default_progression_build"]),
            boss=True,
        )
        _, boss_damage = self.enemy_stats(stage, True)
        minimum_hits_possible = battle["max_hp"] / max(1, boss_damage)
        return (
            battle["won"]
            and battle["duration"] <= float(combat["maximum_boss_kill_seconds"])
            and minimum_hits_possible >= int(combat["minimum_boss_hits_survived"])
        )

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

    def compare_builds(self, stage: int, *, day: int | None = None, monte_carlo: bool = True) -> dict[str, Any]:
        """Compare configured builds using the current gear and hero state."""
        day = self.state.day if day is None else day
        rows = []
        for build_name in self.config["builds"]:
            normal = self.simulate_battle(stage, build_name, boss=False, day=day)
            boss = self.simulate_battle(stage, build_name, boss=True, day=day)
            full_stage = self.simulate_full_stage(stage, build_name, day=day)
            companion_contributions = {}
            for companion_id in boss["active_companions"]:
                without = self.simulate_battle(
                    stage, build_name, boss=True, day=day,
                    excluded_companion=companion_id,
                )
                companion_contributions[companion_id] = {
                    "dps_delta": round(boss["total_dps"] - without["total_dps"], 3),
                    "max_hp_delta": round(boss["max_hp"] - without["max_hp"], 3),
                    "healing_delta": round(boss["post_victory_healing"] - without["post_victory_healing"], 3),
                    "boss_time_delta": round(without["duration"] - boss["duration"], 3),
                }
            skill_contributions = {
                skill_id: {
                    "damage": round(boss["damage_by_source"].get(skill_id, 0.0), 3),
                    "dps": round(boss["skill_dps"].get(skill_id, 0.0), 3),
                    "uses": boss["skill_uses"].get(skill_id, 0),
                }
                for skill_id in boss["active_skills"]
            }
            row = {
                "build": build_name,
                "active_skills": boss["active_skills"],
                "active_companions": boss["active_companions"],
                "normal_kill_seconds": round(normal["duration"], 3),
                "boss_kill_seconds": round(boss["duration"], 3),
                "boss_won": boss["won"],
                "boss_survived": boss["survived"],
                "total_dps": round(boss["total_dps"], 3),
                "normal_attack_damage": round(boss["normal_attack_damage"], 3),
                "expected_crit_multiplier": round(boss["expected_crit_multiplier"], 4),
                "max_hp": round(boss["max_hp"]),
                "effective_hp": round(boss["effective_hp"]),
                "post_victory_healing": round(boss["post_victory_healing"]),
                "single_boss_remaining_hp_percent": round(boss["hero_hp_after_battle"] / boss["max_hp"] * 100, 3),
                "full_stage": {
                    key: round(value, 3) if isinstance(value, float) else value
                    for key, value in full_stage.items() if key != "build"
                },
                "poison_ticks_landed": boss["poison_ticks_landed"],
                "skill_intervals": {key: round(value, 3) for key, value in boss["skill_intervals"].items()},
                "skill_contributions": skill_contributions,
                "companion_contributions": companion_contributions,
            }
            if monte_carlo:
                row["monte_carlo"] = self.monte_carlo_battle(stage, build_name, boss=True, day=day)
            rows.append(row)

        best_dps = max(rows, key=lambda row: row["total_dps"])
        max_dps = max(1.0, max(row["total_dps"] for row in rows))
        maximums = {
            key: max(1.0, max(row["full_stage"][key] for row in rows))
            for key in ("entling_healing", "shield_absorbed", "shield_damage_reduced", "hero_damage_taken")
        }
        completed_times = [row["full_stage"]["total_time"] for row in rows if row["full_stage"]["completed"]]
        fastest_time = min(completed_times) if completed_times else 1.0
        total_fights = int(self.config["combat"]["enemies_per_stage"]) + 1
        for row in rows:
            full_stage = row["full_stage"]
            completion = full_stage["battles_completed"] / total_fights
            death_factor = 1 / (1 + full_stage["deaths"])
            prevented = full_stage["shield_absorbed"] + full_stage["shield_damage_reduced"]
            max_prevented = maximums["shield_absorbed"] + maximums["shield_damage_reduced"]
            row["defensive_score"] = round(
                death_factor * (
                    0.30 * completion
                    + 0.15 * full_stage["remaining_hp_after_boss_percent"] / 100
                    + 0.10 * full_stage["hp_before_boss_percent"] / 100
                    + 0.10 * full_stage["entling_healing"] / maximums["entling_healing"]
                    + 0.10 * prevented / max(1.0, max_prevented)
                    + 0.05 * full_stage["hero_damage_taken"] / maximums["hero_damage_taken"]
                    + 0.20 * (fastest_time / max(fastest_time, full_stage["total_time"]))
                ),
                5,
            )
        best_survival = max(rows, key=lambda row: row["defensive_score"])
        max_survival = max(1e-9, max(row["defensive_score"] for row in rows))
        dps_weight = float(self.config["build_simulation"]["universal_score_dps_weight"])
        survival_weight = float(self.config["build_simulation"]["universal_score_survival_weight"])
        for row in rows:
            row["universal_score"] = round(
                dps_weight * row["total_dps"] / max_dps
                + survival_weight * row["defensive_score"] / max_survival,
                4,
            )
        best_universal = max(rows, key=lambda row: row["universal_score"])
        winning_times = [row["boss_kill_seconds"] for row in rows if row["boss_won"]]
        return {
            "profile": self.profile_name,
            "day": day,
            "hero_level": self.state.hero_level,
            "max_stage_reached": self.state.max_stage,
            "stage": stage,
            "best_dps": best_dps["build"],
            "best_survival": best_survival["build"],
            "best_universal": best_universal["build"],
            "defensive_score_inputs": [
                "battles_completed", "deaths", "remaining_hp_after_boss_percent",
                "hp_before_boss_percent", "entling_healing", "shield_absorbed",
                "shield_damage_reduced", "hero_damage_taken", "total_time",
            ],
            "boss_kill_time_spread": round(max(winning_times) - min(winning_times), 3) if winning_times else None,
            "cannot_survive_boss": [row["build"] for row in rows if not row["boss_survived"]],
            "random_combination_monte_carlo": (
                self.monte_carlo_random_builds(stage, day=day)
                if monte_carlo else None
            ),
            "builds": rows,
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


def build_comparison_reports(config: dict[str, Any], *, monte_carlo: bool = True) -> dict[str, Any]:
    """Generate configured profile/day/stage reports without touching game data."""
    reports: dict[str, Any] = {}
    progression_impact: dict[str, Any] = {}
    stage_1000_days: dict[str, Any] = {}
    stages = [int(stage) for stage in config["build_simulation"]["stages"]]
    for profile, days in config["build_simulation"]["report_days"].items():
        simulator = GearProgressionSimulator(config, profile, pity=True)
        requested = {int(day) for day in days}
        profile_reports = {}
        for _ in range(max(requested)):
            simulator.run_day()
            if simulator.state.day in requested:
                profile_reports[str(simulator.state.day)] = [
                    simulator.compare_builds(stage, day=simulator.state.day, monte_carlo=monte_carlo)
                    for stage in stages
                ]
        reports[profile] = profile_reports
        profile_impact = {}
        profile_stage_1000_days = {}
        for build_name in config["builds"]:
            build_config = copy.deepcopy(config)
            build_config["build_simulation"]["default_progression_build"] = build_name
            build_simulator = GearProgressionSimulator(build_config, profile, pity=True)
            build_days = {}
            reached_day = None
            for _ in range(max(requested)):
                build_simulator.run_day()
                if build_simulator.state.max_stage >= int(config["simulation"]["max_stage"]) and reached_day is None:
                    reached_day = build_simulator.state.day
                if build_simulator.state.day in requested:
                    build_days[str(build_simulator.state.day)] = {
                        "max_stage": build_simulator.state.max_stage,
                        "soft_wall_days_total": build_simulator.state.soft_wall_days,
                        "longest_soft_wall_days": build_simulator.state.longest_wall_days,
                    }
            profile_impact[build_name] = build_days
            profile_stage_1000_days[build_name] = reached_day
        progression_impact[profile] = profile_impact
        stage_1000_days[profile] = profile_stage_1000_days
    categories = {"dps": "best_dps", "defensive": "best_survival", "universal": "best_universal"}
    companion_ids = [key for key, value in config["companions"].items() if isinstance(value, dict)]
    meta = {}
    warnings = []
    for category, result_key in categories.items():
        counts = {companion_id: 0 for companion_id in companion_ids}
        selections = 0
        for profile_days in reports.values():
            for comparisons in profile_days.values():
                for comparison in comparisons:
                    selections += 1
                    winner = comparison[result_key]
                    row = next(row for row in comparison["builds"] if row["build"] == winner)
                    for companion_id in row["active_companions"]:
                        counts[companion_id] += 1
        frequencies = {
            companion_id: round(count / max(1, selections), 4)
            for companion_id, count in counts.items()
        }
        meta[category] = {"best_build_selections": selections, "counts": counts, "frequencies": frequencies}
        warnings.extend(
            f"{companion_id} appears in {frequency:.1%} of best {category} builds"
            for companion_id, frequency in frequencies.items() if frequency > 0.85
        )

    target = GearProgressionSimulator(config, "f2p", pity=True)
    target.run(365)
    target_stage = max(1, min(int(config["simulation"]["max_stage"]), target.state.max_stage))
    windows = {}
    for seconds in (5, 20, 60, 120, 180):
        burst = target.damage_over_duration("burst", seconds, day=365)
        spam = target.damage_over_duration("skill_spam", seconds, day=365)
        windows[str(seconds)] = {
            "burst_dps": round(burst["dps"], 3),
            "skill_spam_dps": round(spam["dps"], 3),
            "winner": "burst" if burst["dps"] > spam["dps"] else "skill_spam",
            "skill_spam_difference_percent": round((spam["dps"] / max(1e-9, burst["dps"]) - 1) * 100, 3),
        }
    basic_20 = target.damage_over_duration("basic_attack", 20, day=365)
    burst_20 = target.damage_over_duration("burst", 20, day=365)
    basic_attack_vs_burst = {
        "duration": 20,
        "basic_attack_dps": round(basic_20["dps"], 3),
        "burst_dps": round(burst_20["dps"], 3),
        "basic_attack_deficit_percent": round((1 - basic_20["dps"] / burst_20["dps"]) * 100, 3),
    }

    companion_damage = {}
    temporary_builds = []
    try:
        for companion_id in ("forest_sprite", "spore_beetle", "thorn_wolf"):
            build_name = f"__compare_{companion_id}__"
            temporary_builds.append(build_name)
            config["builds"][build_name] = {
                "skills": ["spore_strike", "poison_cloud"], "companions": [companion_id]
            }
            result = target.damage_over_duration(build_name, 60, day=365)
            companion_damage[companion_id] = {
                "dps": round(result["dps"], 3),
                "normal_attack_damage": round(result["normal_attack"], 3),
                "skill_damage": round(result.get("spore_strike", 0) + result.get("poison_cloud", 0), 3),
            }
    finally:
        for build_name in temporary_builds:
            config["builds"].pop(build_name, None)

    companion_cfg = config["companions"]
    effectiveness = {}
    for level in (1, 10, 20):
        effectiveness[str(level)] = {
            "forest_sprite": {
                "attack_and_spore_strike_percent": round(level * float(companion_cfg["forest_sprite"]["per_level"]) * 100, 2),
                "poison_cloud_percent": round(level * float(companion_cfg["forest_sprite"]["per_level"]) * float(companion_cfg["forest_sprite"]["poison_cloud_effectiveness"]) * 100, 2),
            },
            "spore_beetle": {
                "normal_attack_base_damage_percent": round(level * float(companion_cfg["spore_beetle"]["per_level"]) * 100, 2),
                "attack_speed_percent": round(min(float(companion_cfg["spore_beetle"]["maximum_attack_speed_bonus"]), level * float(companion_cfg["spore_beetle"]["attack_speed_per_level"])) * 100, 2),
                "can_crit": True,
            },
            "thorn_wolf": {
                "critical_chance_points": round(level * float(companion_cfg["thorn_wolf"]["percentage_points_per_level"]), 2),
                "critical_damage_percent": round(level * float(companion_cfg["thorn_wolf"]["critical_damage_percent_per_level"]), 2),
            },
            "mushroom_owl": {"cooldown_reduction_percent": round(min(float(companion_cfg["mushroom_owl"]["maximum_reduction"]), level * float(companion_cfg["mushroom_owl"]["per_level"])) * 100, 2)},
            "ancient_entling": {
                "normal_post_victory_heal_percent": round(level * float(companion_cfg["ancient_entling"]["normal_ratio_per_level"]) * 100, 2),
                "boss_post_victory_heal_percent": round(level * float(companion_cfg["ancient_entling"]["normal_ratio_per_level"]) * float(companion_cfg["ancient_entling"]["boss_multiplier"]) * 100, 2),
            },
            "baby_slime": {"max_hp_percent": round(level * float(companion_cfg["baby_slime"]["per_level"]) * 100, 2)},
        }

    skill_growth = float(config["skills"]["power_per_level"])
    skill_level_multipliers = {
        str(level): round(1 + skill_growth * (level - 1), 4)
        for level in (1, 5, 10, 15, 20)
    }
    equipment_role = {}
    role_simulator = GearProgressionSimulator(config, "f2p", pity=True)
    no_companion_build = "__equipment_role_no_companions__"
    config["builds"][no_companion_build] = {
        "skills": list(config["builds"]["balanced"]["skills"]), "companions": []
    }
    try:
        for _ in range(365):
            role_simulator.run_day()
            if role_simulator.state.day not in {30, 90, 365}:
                continue
            day = role_simulator.state.day
            stats = role_simulator.combat_stats(day, "balanced")
            full = role_simulator.damage_over_duration("balanced", 60, day=day)
            without_companions = role_simulator.damage_over_duration(no_companion_build, 60, day=day)
            equipment_role[str(day)] = {
                "build": "balanced", "window_seconds": 60,
                "hero_base_damage": round(stats["base_damage"], 3),
                "equipment_damage": round(stats["gear_damage"], 3),
                "hero_base_hp": round(stats["base_hp"], 3),
                "equipment_hp": round(stats["gear_hp"], 3),
                "skill_dps": round(full.get("spore_strike", 0) / 60 + full.get("poison_cloud", 0) / 60, 3),
                "companion_dps_contribution": round(full["dps"] - without_companions["dps"], 3),
                "total_dps": round(full["dps"], 3),
            }
    finally:
        config["builds"].pop(no_companion_build, None)

    pairs = {}
    for left, right in (("tank", "sustain"), ("balanced", "burst")):
        pairs[f"{left}_vs_{right}"] = {}
        for name in (left, right):
            battle = target.simulate_battle(target_stage, name, boss=True, day=365)
            pairs[f"{left}_vs_{right}"][name] = {
                "boss_kill_seconds": round(battle["duration"], 3), "won": battle["won"],
                "dps": round(battle["total_dps"], 3), "effective_hp": round(battle["effective_hp"]),
                "post_victory_healing": round(battle["post_victory_healing"]),
            }
    single_boss = {}
    full_stage = {}
    for name in config["builds"]:
        battle = target.simulate_battle(target_stage, name, boss=True, day=365)
        single_boss[name] = {
            "won": battle["won"], "duration": round(battle["duration"], 3),
            "dps": round(battle["total_dps"], 3), "max_hp": round(battle["max_hp"]),
            "effective_hp": round(battle["effective_hp"]),
            "remaining_hp_percent": round(battle["hero_hp_after_battle"] / battle["max_hp"] * 100, 3),
            "shield_absorbed": round(battle["shield_absorbed"]),
            "shield_damage_reduced": round(battle["shield_damage_reduced"]),
            "entling_healing_after_victory": round(battle["post_victory_healing"]),
        }
        full_stage[name] = {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in target.simulate_full_stage(target_stage, name, day=365).items()
            if key != "build"
        }
    return {
        "comparisons": reports, "progression_impact": progression_impact,
        "targeted_comparisons": {
            "f2p_day_365_stage": target_stage,
            "forest_vs_beetle_vs_wolf": companion_damage,
            "burst_vs_skill_spam_by_duration": windows,
            "basic_attack_vs_burst": basic_attack_vs_burst,
            "single_boss": single_boss,
            "full_stage": full_stage,
            **pairs,
            "companion_effectiveness_by_level": effectiveness,
            "skill_level_multipliers": skill_level_multipliers,
            "equipment_role_f2p": equipment_role,
        },
        "companion_meta_frequency": meta, "meta_warnings": warnings,
        "stage_1000_days": stage_1000_days,
    }


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
    parser.add_argument("--compare-builds", action="store_true")
    parser.add_argument("--no-combat-monte-carlo", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.compare_builds:
        result = build_comparison_reports(
            config,
            monte_carlo=not args.no_combat_monte_carlo,
        )
    elif args.compare_pity:
        result = pity_comparison(config, days=args.days)
    elif args.profile == "all":
        result = simulate_all(config, days=args.days, pity=not args.no_pity)
    else:
        result = {args.profile: GearProgressionSimulator(config, args.profile, pity=not args.no_pity).run(args.days)}
    if args.as_json or args.compare_pity or args.compare_builds:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
