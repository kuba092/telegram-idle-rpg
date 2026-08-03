"""Server-authoritative quests, achievements and first-clear rewards.

Daily/weekly/achievement rewards are claimed manually.  First-clear stage rewards
are credited automatically by the same SQLite transaction that advances a stage.
The module deliberately contains no HTTP or FastAPI code.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any, Mapping

QUEST_VERSION = 1
DAILY_PREMIUM_CAP = 15
WEEKLY_PREMIUM_CAP = 40
RESOURCE_FIELDS = {"premium_crystals", "skill_tomes", "companion_essence", "salvage_dust", "refinement_ore"}

def _q(qid, title, description, objective, target, reward):
    return {"quest_id": qid, "title": title, "description": description,
            "objective_type": objective, "target_value": target, "reward": reward}

DAILY_CATALOG = (
    _q("daily_enemies_20", "Тихая охота", "Победите 20 врагов", "defeat_enemies", 20, {"skill_tomes": 3}),
    _q("daily_chests_10", "Звон крышек", "Откройте 10 сундуков", "open_chests", 10, {"salvage_dust": 5}),
    _q("daily_salvage_8", "Полезный лом", "Разберите 8 предметов", "salvage_items", 8, {"refinement_ore": 2}),
    _q("daily_skill_1", "Новая руна", "Улучшите навык 1 раз", "upgrade_skills", 1, {"companion_essence": 3}),
    _q("daily_boss_1", "Большая шляпка", "Победите 1 босса", "defeat_bosses", 1, {"premium_crystals": 5}),
    _q("daily_login", "Утренний сбор", "Войдите в игру", "login", 1, {"premium_crystals": 2}),
)
WEEKLY_CATALOG = (
    _q("weekly_enemies_300", "Долгий поход", "Победите 300 врагов", "defeat_enemies", 300, {"skill_tomes": 15}),
    _q("weekly_elites_15", "Редкие споры", "Победите 15 элитных врагов", "defeat_elites", 15, {"companion_essence": 15}),
    _q("weekly_bosses_10", "Совет великанов", "Победите 10 боссов", "defeat_bosses", 10, {"refinement_ore": 8}),
    _q("weekly_chests_100", "Сто находок", "Откройте 100 сундуков", "open_chests", 100, {"salvage_dust": 25}),
    _q("weekly_skills_10", "Школа магии", "Улучшите навыки 10 раз", "upgrade_skills", 10, {"skill_tomes": 10}),
    _q("weekly_companions_10", "Верные друзья", "Улучшите спутников 10 раз", "upgrade_companions", 10, {"companion_essence": 10}),
)
DAILY_MILESTONES = {20: {"salvage_dust": 5}, 40: {"skill_tomes": 3, "companion_essence": 3}, 60: {"premium_crystals": 8}}
WEEKLY_MILESTONES = {40: {"skill_tomes": 10}, 80: {"companion_essence": 10, "refinement_ore": 10}, 120: {"premium_crystals": 40}}

def _achievement_catalog():
    out = []
    for value, crystals in ((10,10),(25,15),(50,25),(100,40),(250,60),(500,100)):
        out.append(_q(f"achievement_stage_{value}", f"Этап {value}", f"Достигните этапа {value}", "reach_stage", value, {"premium_crystals": crystals}))
    for value, crystals in ((5,5),(10,10),(15,15),(20,25),(25,40),(30,60)):
        out.append(_q(f"achievement_chest_{value}", f"Сундук {value}", f"Повысьте сундук до уровня {value}", "reach_chest_level", value, {"premium_crystals": crystals}))
    for value, crystals in ((100,5),(1000,15),(10000,40),(100000,100)):
        out.append(_q(f"achievement_enemies_{value}", f"Охотник: {value}", f"Победите {value} врагов", "defeat_enemies", value, {"premium_crystals": crystals}))
    for kind, label in (("skill", "навыков"), ("companion", "спутников")):
        objective = "upgrade_skills" if kind == "skill" else "upgrade_companions"
        for value, crystals in ((25,10),(100,20),(250,40)):
            out.append(_q(f"achievement_{kind}_levels_{value}", f"Знаток {label}: {value}", f"Получите суммарно {value} уровней {label}", objective, value, {"premium_crystals": crystals}))
        out.append(_q(f"achievement_{kind}_master", f"Первый мастер {label}", f"Впервые достигните максимального уровня", objective, 1, {"premium_crystals": 75}) | {"mastery": True})
    for value, crystals in ((100,10),(1000,25)):
        out.append(_q(f"achievement_salvage_{value}", f"Переработчик: {value}", f"Разберите {value} предметов", "salvage_items", value, {"premium_crystals": crystals}))
    for value, crystals in ((25,10),(100,30)):
        out.append(_q(f"achievement_reroll_{value}", f"Искатель свойств: {value}", f"Переработайте свойства {value} раз", "reroll_items", value, {"premium_crystals": crystals}))
    return tuple(out)

ACHIEVEMENT_CATALOG = _achievement_catalog()
FIRST_CLEAR_REWARDS = {10: 5, 25: 10, 50: 15, 100: 25, 250: 40, 500: 75}
EVENT_OBJECTIVES = {
    "enemy_defeated": "defeat_enemies", "elite_defeated": "defeat_elites",
    "boss_defeated": "defeat_bosses", "chest_opened": "open_chests",
    "item_salvaged": "salvage_items", "item_rerolled": "reroll_items",
    "skill_upgraded": "upgrade_skills", "companion_upgraded": "upgrade_companions",
    "chest_xp_gained": "gain_chest_xp", "stage_reached": "reach_stage",
    "chest_level_reached": "reach_chest_level", "skill_tomes_gained": "collect_skill_tomes",
    "companion_essence_gained": "collect_companion_essence", "player_login": "login",
    "gold_spent": "spend_gold",
}

def utc_now(now=None):
    if now is None: return dt.datetime.now(dt.timezone.utc)
    if isinstance(now, (int, float)): return dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)

def daily_key(now=None): return utc_now(now).strftime("%Y-%m-%d")
def weekly_key(now=None):
    iso = utc_now(now).isocalendar(); return f"{iso.year}-W{iso.week:02d}"
def reset_times(now=None):
    current = utc_now(now)
    daily = (current + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    weekly = (current + dt.timedelta(days=7-current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return daily, weekly

def _loads(value, default):
    if isinstance(value, type(default)): return copy.deepcopy(value)
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else copy.deepcopy(default)
    except (TypeError, ValueError): return copy.deepcopy(default)

def _instances(catalog, quest_type, reset_at, created_at):
    return [{**copy.deepcopy(q), "quest_type": quest_type, "current_value": 0,
             "completed": False, "claimed": False, "reset_at": reset_at,
             "created_at": created_at, "completed_at": None, "claimed_at": None,
             "version": QUEST_VERSION} for q in catalog]

def ensure_state(connection, player_id, now=None):
    """Lazily reset periods. Must be called inside the caller's transaction."""
    current = utc_now(now); now_s = current.isoformat(); daily_reset, weekly_reset = reset_times(current)
    row = connection.execute("SELECT * FROM players WHERE telegram_id=?", (player_id,)).fetchone()
    if row is None: return
    p = dict(row); updates = {}
    if str(p.get("quest_daily_reset_key") or "") != daily_key(current):
        updates["quest_daily_reset_key"] = daily_key(current)
        updates["daily_quests_json"] = json.dumps({"quests": _instances(DAILY_CATALOG, "daily", daily_reset.isoformat(), now_s), "claimed_milestones": []}, ensure_ascii=False)
    if str(p.get("quest_weekly_reset_key") or "") != weekly_key(current):
        updates["quest_weekly_reset_key"] = weekly_key(current)
        updates["weekly_quests_json"] = json.dumps({"quests": _instances(WEEKLY_CATALOG, "weekly", weekly_reset.isoformat(), now_s), "claimed_milestones": []}, ensure_ascii=False)
    if not p.get("achievements_json"):
        updates["achievements_json"] = json.dumps({"quests": _instances(ACHIEVEMENT_CATALOG, "achievement", None, now_s)}, ensure_ascii=False)
    if updates:
        sets = ",".join(f"{key}=?" for key in updates)
        connection.execute(f"UPDATE players SET {sets} WHERE telegram_id=?", (*updates.values(), player_id))

def _state(player, field): return _loads(player.get(field), {"quests": [], "claimed_milestones": []})

class QuestProgressTracker:
    """Single transactional event dispatcher. action ids are durably deduplicated."""
    def __init__(self, connection, player_id, now=None):
        self.connection, self.player_id, self.now = connection, int(player_id), utc_now(now)

    def dispatch(self, event, amount=1, *, client_action_id=None, absolute=False,
                 mastered=False, achievement_amount=None):
        if event not in EVENT_OBJECTIVES: raise ValueError(f"unknown quest event: {event}")
        ensure_state(self.connection, self.player_id, self.now)
        row = dict(self.connection.execute("SELECT * FROM players WHERE telegram_id=?", (self.player_id,)).fetchone())
        history = _loads(row.get("quest_claim_history_json"), {})
        event_key = f"event:{event}:{client_action_id}" if client_action_id else None
        if event_key and event_key in history: return False
        objective = EVENT_OBJECTIVES[event]; changed = False; stamp = self.now.isoformat()
        for field in ("daily_quests_json", "weekly_quests_json", "achievements_json"):
            state = _state(row, field)
            for quest in state["quests"]:
                if quest.get("objective_type") != objective: continue
                if quest.get("mastery") and not mastered: continue
                event_amount = achievement_amount if field == "achievements_json" and achievement_amount is not None else amount
                event_absolute = absolute or (field == "achievements_json" and achievement_amount is not None)
                old = int(quest.get("current_value", 0)); value = max(old, int(event_amount)) if event_absolute else old + max(0, int(event_amount))
                if event == "player_login": value = min(1, value)
                quest["current_value"] = value
                if not quest.get("completed") and value >= int(quest["target_value"]):
                    quest["completed"], quest["completed_at"] = True, stamp
                changed |= value != old
            self.connection.execute(f"UPDATE players SET {field}=? WHERE telegram_id=?", (json.dumps(state, ensure_ascii=False), self.player_id))
        if event_key:
            history[event_key] = {"at": stamp}
            if len(history) > 2000:
                history = dict(list(history.items())[-1500:])
            self.connection.execute("UPDATE players SET quest_claim_history_json=? WHERE telegram_id=?", (json.dumps(history), self.player_id))
        return changed

    def first_clear(self, previous_stage, reached_stage):
        ensure_state(self.connection, self.player_id, self.now)
        row = dict(self.connection.execute("SELECT quest_claim_history_json FROM players WHERE telegram_id=?", (self.player_id,)).fetchone())
        history = _loads(row.get("quest_claim_history_json"), {}); gained, claimed = 0, []
        for stage, reward in FIRST_CLEAR_REWARDS.items():
            key = f"first_clear:{stage}"
            if int(previous_stage) < stage <= int(reached_stage) and key not in history:
                history[key] = {"at": self.now.isoformat(), "reward": {"premium_crystals": reward}}
                gained += reward; claimed.append(stage)
        if gained:
            self.connection.execute("UPDATE players SET premium_crystals=premium_crystals+?,quest_claim_history_json=? WHERE telegram_id=?", (gained, json.dumps(history), self.player_id))
        return {"premium_crystals_gained": gained, "claimed_milestones": claimed}

def activity(state, per_quest): return sum(per_quest for q in state.get("quests", []) if q.get("completed"))

def public_block(player, now=None):
    current = utc_now(now); dr, wr = reset_times(current)
    daily, weekly, achievements = (_state(player, f) for f in ("daily_quests_json", "weekly_quests_json", "achievements_json"))
    history = _loads(player.get("quest_claim_history_json"), {})
    def period(state, points, milestones):
        ap = activity(state, points)
        return {"quests": state["quests"], "activity_points": ap,
                "milestones": [{"milestone_points": p, "reward": r, "completed": ap >= p, "claimed": p in state.get("claimed_milestones", [])} for p,r in milestones.items()],
                "completed_count": sum(bool(q.get("completed")) for q in state["quests"]),
                "claimable_count": sum(bool(q.get("completed")) and not q.get("claimed") for q in state["quests"])}
    stages = sorted(int(k.split(":")[-1]) for k in history if k.startswith("first_clear:"))
    next_stage = next((s for s in FIRST_CLEAR_REWARDS if s not in stages), None)
    aq = achievements["quests"]
    return {"server_time": current.isoformat(), "daily_reset_at": dr.isoformat(), "weekly_reset_at": wr.isoformat(),
            "daily": period(daily, 10, DAILY_MILESTONES), "weekly": period(weekly, 20, WEEKLY_MILESTONES),
            "achievements": {"quests": aq, "completed_count": sum(bool(q.get("completed")) for q in aq), "claimed_count": sum(bool(q.get("claimed")) for q in aq), "claimable_count": sum(bool(q.get("completed")) and not q.get("claimed") for q in aq)},
            "first_clear": {"claimed_milestones": stages, "next_milestone": next_stage},
            "currency_sources": {"premium_crystals": {"paid_currency": True, "repeatable_farming": False, "free_sources": ["daily_quests", "weekly_quests", "achievements", "first_clear", "future_events", "future_compensation"]}}}

def reward_totals(rewards):
    totals = {key: 0 for key in RESOURCE_FIELDS}
    for reward in rewards:
        for key, value in reward.items():
            if key not in RESOURCE_FIELDS or int(value) < 0: raise ValueError("invalid quest reward")
            totals[key] += int(value)
    return {k:v for k,v in totals.items() if v}
