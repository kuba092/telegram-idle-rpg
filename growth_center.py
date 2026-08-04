"""Server-authoritative daily-growth aggregation.

The module is deliberately HTTP/database agnostic.  Callers provide normalized
collections/catalogs while all economic numbers come from the existing systems.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any, Mapping

from awakening_system import MAX_AWAKENING_TIER, MAX_RANK, STARS_PER_RANK, awakening_cost, normalize_rank_state, star_cost
from equipment_stats import build_score, normalize_item
from loot_progression import chest_progress, inventory_capacity
from offline_progression import public_status as public_offline_status
from progression_systems import (
    MAX_PROGRESSION_LEVEL, companion_gold_cost, companion_upgrade_cost,
    next_milestone, skill_gold_cost, skill_upgrade_cost,
)
from quest_system import public_block as public_quest_block
from summon_system import COSTS as SUMMON_COSTS, normalize_fragments, normalize_state as normalize_summon_state

CACHE_TTL_SECONDS = 10
_CACHE: dict[int, tuple[float, dict]] = {}
_CACHE_LOCK = threading.RLock()

URGENCY = {"low": 10, "normal": 30, "info": 40, "attention": 70, "critical": 100}
ROUTES = {
    "offline": "/offline/claim", "quests": "/quests/claim-all", "chest": "/chest/upgrade",
    "pending_loot": "/loot/equip", "skill_upgrade": "/skills/upgrade", "companion_upgrade": "/companions/upgrade",
    "skill_rank": "/skills/rank-up", "companion_rank": "/companions/rank-up",
    "skill_awakening": "/skills/awaken", "companion_awakening": "/companions/awaken",
    "summon_skill": "/summon/skill", "summon_companion": "/summon/companion",
    "battle": "/attack", "boss": "/boss/start", "elite": "/attack", "loot_compare": "/loot/equip",
}


def invalidate_growth_center(player_id: int) -> None:
    with _CACHE_LOCK:
        _CACHE.pop(int(player_id), None)


def clear_growth_center_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _cached(player_id: int, now: float) -> dict | None:
    with _CACHE_LOCK:
        entry = _CACHE.get(int(player_id))
        if entry and 0 <= now - entry[0] < CACHE_TTL_SECONDS:
            return copy.deepcopy(entry[1])
        if entry:
            _CACHE.pop(int(player_id), None)
    return None


def _put(player_id: int, now: float, value: dict) -> None:
    with _CACHE_LOCK:
        _CACHE[int(player_id)] = (now, copy.deepcopy(value))


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return copy.deepcopy(value)
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else copy.deepcopy(default)
    except (TypeError, ValueError):
        return copy.deepcopy(default)


def blocker(resource: str, required: Any, current: Any, related_action: str, message: str | None = None) -> dict:
    required_i, current_i = max(0, _int(required)), max(0, _int(current))
    missing = max(0, required_i - current_i)
    labels = {
        "gold": "золота", "skill_tomes": "томов навыков", "companion_essence": "эссенции спутников",
        "skill_fragments": "фрагментов навыка", "companion_fragments": "фрагментов спутника",
        "salvage_dust": "пыли разбора", "refinement_ore": "руды улучшения",
        "inventory_space": "свободных мест", "skill_summon_scrolls": "свитков призыва навыков",
        "companion_summon_contracts": "контрактов призыва спутников", "premium_crystals": "кристаллов",
        "rank_requirement": "ранга", "level_requirement": "уровней", "locked": "разблокировки",
        "cooldown": "секунд", "no_claimable_reward": "доступных наград",
    }
    return {"resource": resource, "required": required_i, "current": current_i, "missing": missing,
            "message": message or f"Не хватает {missing} {labels.get(resource, resource)}", "related_action": related_action}


def detect_better_items(inventory: list, equipment: Mapping[str, Any], profile: str, limit: int = 100) -> dict:
    best = None; count = 0; scanned = 0
    equipped_ids = {str(item.get("item_id")) for item in equipment.values() if isinstance(item, Mapping)}
    for raw in list(inventory)[:min(100, max(0, int(limit)))]:
        scanned += 1
        item = normalize_item(raw)
        item_id = str(item.get("item_id") or "")
        if item_id and item_id in equipped_ids:
            continue
        slot = str(item.get("slot") or item.get("slot_key") or "")
        if not slot:
            continue
        equipped = normalize_item(equipment.get(slot) or {})
        candidate_score, equipped_score = build_score(item, profile), build_score(equipped, profile)
        delta = round(candidate_score - equipped_score, 2)
        if delta <= 0:
            continue
        count += 1
        candidate = {"item_id": item_id, "slot": slot, "build_score": candidate_score,
                     "equipped_build_score": equipped_score, "build_score_delta": delta,
                     "locked": bool(item.get("locked", item.get("is_locked", False))), "comparison_profile": profile}
        if best is None or (-delta, item_id) < (-best["build_score_delta"], best["item_id"]):
            best = candidate
    return {"count": count, "best_item": best, "scanned_count": scanned, "scan_limit": 100,
            "comparison_profile": profile, "auto_equipped": False}


def _entity_candidates(kind: str, collection: Mapping[str, Any], active_ids: set[str], catalog: Mapping[str, Any],
                       fragments_raw: Any, player: Mapping[str, Any]) -> dict:
    fragment_key = f"{kind}_fragments"; fragments = normalize_fragments(fragments_raw, set(catalog))
    resource = "skill_tomes" if kind == "skill" else "companion_essence"
    amount = max(0, _int(player.get(resource))); gold = max(0, _int(player.get("gold")))
    upgrades, ranks, awakenings = [], [], []
    for entity_id, raw in collection.items():
        if entity_id not in catalog or not isinstance(raw, Mapping) or not raw.get("owned", True):
            continue
        level = max(1, min(MAX_PROGRESSION_LEVEL, _int(raw.get("level"), 1)))
        active = entity_id in active_ids
        if level < MAX_PROGRESSION_LEVEL:
            material_cost = skill_upgrade_cost(level) if kind == "skill" else companion_upgrade_cost(level)
            gold_cost = skill_gold_cost(level) if kind == "skill" else companion_gold_cost(level)
            if amount >= material_cost and gold >= gold_cost:
                milestone_next = next_milestone(level)
                upgrades.append({"entity_id": entity_id, "level": level, "active": active,
                    "milestone_next": milestone_next, "milestone_on_next_level": milestone_next == level + 1,
                    "cost": {resource: material_cost, "gold": gold_cost},
                    "sort": (not active, milestone_next != level + 1, material_cost + gold_cost, entity_id)})
        rarity = str(catalog[entity_id].get("rarity", "common")); state = normalize_rank_state(raw)
        balance = fragments.get(entity_id, 0); rank_cost = star_cost(rarity, state)
        if rank_cost is not None and balance >= rank_cost:
            transition = STARS_PER_RANK - state["rank_stars"]
            ranks.append({"entity_id": entity_id, "active": active, "rank": state["rank"], "rank_stars": state["rank_stars"],
                          "cost": {fragment_key: rank_cost}, "rank_version": state["rank_version"],
                          "sort": (not active, transition, rank_cost, entity_id)})
        awaken_cost = awakening_cost(rarity, state["awakening_tier"]) if state["rank"] >= MAX_RANK else None
        if awaken_cost is not None and balance >= awaken_cost:
            next_tier = state["awakening_tier"] + 1
            awakenings.append({"entity_id": entity_id, "active": active, "next_tier": next_tier,
                               "cost": {fragment_key: awaken_cost}, "rank_version": state["rank_version"],
                               "sort": (-next_tier, not active, awaken_cost, entity_id)})
    for values in (upgrades, ranks, awakenings):
        values.sort(key=lambda item: item["sort"])
        for item in values: item.pop("sort", None)
    return {"affordable_count": len(upgrades), "best_upgrade": upgrades[0] if upgrades else None,
            "rankable_count": len(ranks), "best_rank": ranks[0] if ranks else None,
            "awakenable_count": len(awakenings), "best_awakening": awakenings[0] if awakenings else None}


def _notification(notification_type: str, count: int, severity: str, title: str, message: str,
                  section: str, route: str, *, dismissible: bool = False) -> dict:
    return {"notification_id": notification_type, "type": notification_type, "count": max(1, count),
            "severity": severity, "title": title, "message": message, "target_section": section,
            "target_route": route, "dismissible": dismissible, "persistent": not dismissible}


def _recommendation(action_id: str, category: str, available: bool, reason: str, urgency: str, score: int,
                    requirements: dict | None = None, resources: dict | None = None, **extra) -> dict:
    requirements, resources = requirements or {}, resources or {}
    missing = {key: max(0, _int(value) - _int(resources.get(key))) for key, value in requirements.items()
               if _int(resources.get(key)) < _int(value)}
    return {"action_id": action_id, "category": category, "available": bool(available), "reason": reason,
            "target_route": ROUTES[category], "resource_requirements": requirements,
            "current_resources": {key: max(0, _int(resources.get(key))) for key in requirements},
            "missing_resources": missing, "urgency": URGENCY[urgency], "score": score, **extra}


class GrowthCenter:
    def __init__(self, player: Mapping[str, Any], *, skill_collection: Mapping[str, Any], companion_collection: Mapping[str, Any],
                 skill_slots: list, companion_slots: list, skill_catalog: Mapping[str, Any], companion_catalog: Mapping[str, Any],
                 inventory: list, equipment: Mapping[str, Any], now: float | None = None):
        self.player, self.now = dict(player), float(time.time() if now is None else now)
        self.skills, self.companions = skill_collection, companion_collection
        self.skill_slots, self.companion_slots = skill_slots, companion_slots
        self.skill_catalog, self.companion_catalog = skill_catalog, companion_catalog
        self.inventory, self.equipment = inventory, equipment

    def build(self) -> dict:
        p = self.player; now_i = int(self.now)
        quests = public_quest_block(p, self.now); offline = public_offline_status(p, now_i)
        chest = chest_progress(p); capacity = inventory_capacity(_int(p.get("chest_level"), 1))
        inv_count, inv_free = len(self.inventory), max(0, capacity - len(self.inventory))
        pending_loot = p.get("pending_loot")
        has_pending_loot = isinstance(pending_loot, Mapping) and bool(pending_loot)
        profile = str(p.get("comparison_profile", "balanced"))
        better = detect_better_items(self.inventory, self.equipment, profile)
        skills = _entity_candidates("skill", self.skills, {x for x in self.skill_slots if x}, self.skill_catalog,
                                    p.get("skill_fragments_json"), p)
        companions = _entity_candidates("companion", self.companions, {x for x in self.companion_slots if x}, self.companion_catalog,
                                        p.get("companion_fragments_json"), p)
        daily_claim = _int(quests["daily"]["claimable_count"]); weekly_claim = _int(quests["weekly"]["claimable_count"])
        achievements = _int(quests["achievements"]["claimable_count"])
        daily_miles = sum(m["completed"] and not m["claimed"] for m in quests["daily"]["milestones"])
        weekly_miles = sum(m["completed"] and not m["claimed"] for m in quests["weekly"]["milestones"])
        skill_tickets = max(0, _int(p.get("skill_summon_scrolls")))
        companion_tickets = max(0, _int(p.get("companion_summon_contracts")))
        counters = {"claimable_quests": daily_claim + weekly_claim, "claimable_milestones": daily_miles + weekly_miles,
            "claimable_achievements": achievements, "rankable_skills": skills["rankable_count"],
            "rankable_companions": companions["rankable_count"], "awakenable_skills": skills["awakenable_count"],
            "awakenable_companions": companions["awakenable_count"], "affordable_skill_upgrades": skills["affordable_count"],
            "affordable_companion_upgrades": companions["affordable_count"], "skill_ticket_summons": skill_tickets,
            "companion_ticket_summons": companion_tickets, "inventory_count": inv_count, "inventory_capacity": capacity,
            "inventory_free": inv_free, "better_items": better["count"], "offline_claimable": bool(offline["claimable"]),
            "chest_upgrade_ready": bool(chest["chest_upgrade_ready"])}
        recommendations = []
        if has_pending_loot: recommendations.append(_recommendation("resolve_pending_loot", "pending_loot", True, "Решите судьбу найденного предмета", "critical", 1000))
        if offline["claimable"]: recommendations.append(_recommendation("claim_offline", "offline", True, "Офлайн-награда готова", "attention", 950))
        if daily_claim + weekly_claim + achievements + daily_miles + weekly_miles:
            recommendations.append(_recommendation("claim_quest_rewards", "quests", True, "Есть награды заданий", "attention", 900))
        if chest["chest_upgrade_ready"]: recommendations.append(_recommendation("upgrade_chest", "chest", True, "Хватает золота на улучшение сундука", "attention", 850, {"gold": chest["chest_upgrade_gold_cost"]}, p))
        for kind, data in (("skill", skills), ("companion", companions)):
            if data["best_awakening"]: recommendations.append(_recommendation(f"awaken_{kind}", f"{kind}_awakening", True, "Доступно пробуждение", "attention", 800, candidate=data["best_awakening"]))
            if data["best_rank"]: recommendations.append(_recommendation(f"rank_{kind}", f"{kind}_rank", True, "Доступно повышение ранга", "attention", 750, candidate=data["best_rank"]))
            if data["best_upgrade"]:
                c=data["best_upgrade"]; recommendations.append(_recommendation(f"upgrade_{kind}", f"{kind}_upgrade", True, "Доступно улучшение", "info", 700 if kind=="skill" else 680, c["cost"], p, candidate=c))
        if skill_tickets: recommendations.append(_recommendation("summon_skill_ticket", "summon_skill", True, "Есть свиток призыва", "info", 620, {"skill_summon_scrolls": SUMMON_COSTS[1]["ticket"]}, p))
        if companion_tickets: recommendations.append(_recommendation("summon_companion_ticket", "summon_companion", True, "Есть контракт призыва", "info", 610, {"companion_summon_contracts": SUMMON_COSTS[1]["ticket"]}, p))
        boss_available = bool(_int(p.get("boss_waiting"))) and not bool(_int(p.get("game_completed")))
        if boss_available: recommendations.append(_recommendation("start_boss", "boss", True, "Босс этапа доступен", "info", 500))
        can_battle = _int(p.get("hero_hp"), 1) > 0 and not bool(_int(p.get("game_completed"))) and not boss_available
        if can_battle: recommendations.append(_recommendation("continue_battle", "battle", True, "Продолжить прохождение", "normal", 100))
        recommendations.sort(key=lambda x: (-x["urgency"], -x["score"], x["action_id"]))
        notifications = []
        def add(t,c,s,title,msg,section,category):
            if c: notifications.append(_notification(t, int(c), s, title, msg, section, ROUTES[category]))
        add("pending_loot", has_pending_loot, "critical", "Нужно решить предмет", "Наденьте или продайте найденный предмет", "equipment", "pending_loot")
        add("offline_claimable", offline["claimable"], "attention", "Офлайн-награда", "Награда готова к получению", "offline", "offline")
        # Premium quest rewards are represented by their period badge, once per type.
        for qtype, block, ntype in (("daily", quests["daily"], "daily_quest_claimable"),("weekly", quests["weekly"], "weekly_quest_claimable"),("achievement", quests["achievements"], "achievement_claimable")):
            claimable=[q for q in block["quests"] if q.get("completed") and not q.get("claimed")]
            severity="attention" if any(_int(q.get("reward",{}).get("premium_crystals")) > 0 for q in claimable) else "info"
            add(ntype, len(claimable), severity, "Награды заданий", f"Можно получить: {len(claimable)}", "quests", "quests")
        add("daily_milestone_claimable", daily_miles, "attention" if any(m["completed"] and not m["claimed"] and m["reward"].get("premium_crystals") for m in quests["daily"]["milestones"]) else "info", "Дневная веха", "Награда вехи готова", "quests", "quests")
        add("weekly_milestone_claimable", weekly_miles, "attention" if any(m["completed"] and not m["claimed"] and m["reward"].get("premium_crystals") for m in quests["weekly"]["milestones"]) else "info", "Недельная веха", "Награда вехи готова", "quests", "quests")
        add("chest_upgrade_ready", chest["chest_upgrade_ready"], "attention", "Сундук можно улучшить", "Золота достаточно", "chest", "chest")
        for kind,data in (("skill",skills),("companion",companions)):
            label="навыка" if kind=="skill" else "спутника"
            add(f"{kind}_upgrade_available", data["affordable_count"], "info", f"Улучшение {label}", "Ресурсов достаточно", kind+"s", f"{kind}_upgrade")
            add(f"{kind}_rank_available", data["rankable_count"], "attention", f"Новый ранг {label}", "Доступно повышение ранга", kind+"s", f"{kind}_rank")
            add(f"{kind}_awakening_available", data["awakenable_count"], "attention", f"Пробуждение {label}", "Доступно пробуждение", kind+"s", f"{kind}_awakening")
        add("skill_summon_available", skill_tickets, "info", "Призыв навыка", f"Свитков: {skill_tickets}", "summon", "summon_skill")
        add("companion_summon_available", companion_tickets, "info", "Призыв спутника", f"Контрактов: {companion_tickets}", "summon", "summon_companion")
        add("boss_ready", boss_available, "attention", "Босс готов", "Можно начать бой с боссом", "battle", "boss")
        add("elite_active", bool(_int(p.get("elite_active"))), "attention", "Элитный противник", "Элитный противник активен", "battle", "elite")
        notifications.sort(key=lambda x: (-URGENCY[x["severity"]], x["notification_id"]))
        priority = recommendations[0] if recommendations else _recommendation("no_action", "battle", False, "Нет доступных действий", "low", 0)
        titles={"resolve_pending_loot":"Решить предмет","claim_offline":"Забрать офлайн-награду","claim_quest_rewards":"Забрать награды","upgrade_chest":"Улучшить сундук"}
        priority_action={k:priority[k] for k in ("action_id","category","available","reason","target_route")}
        priority_action.update({"title":titles.get(priority["action_id"], priority["reason"]), "description":priority["reason"],
                                "urgency":priority["urgency"], "estimated_value":priority.get("candidate") or {}})
        blockers=[]
        for kind,data in (("skill",skills),("companion",companions)):
            collection = self.skills if kind=="skill" else self.companions
            if data["affordable_count"] == 0 and any(v.get("owned",True) and _int(v.get("level"),1)<MAX_PROGRESSION_LEVEL for v in collection.values()):
                resource="skill_tomes" if kind=="skill" else "companion_essence"; costs=[]; gold_costs=[]
                for v in collection.values():
                    level=_int(v.get("level"),1)
                    if v.get("owned",True) and level<MAX_PROGRESSION_LEVEL:
                        costs.append(skill_upgrade_cost(level) if kind=="skill" else companion_upgrade_cost(level))
                        gold_costs.append(skill_gold_cost(level) if kind=="skill" else companion_gold_cost(level))
                if costs and _int(p.get(resource)) < min(costs): blockers.append(blocker(resource,min(costs),p.get(resource),f"upgrade_{kind}"))
                if gold_costs and _int(p.get("gold")) < min(gold_costs): blockers.append(blocker("gold",min(gold_costs),p.get("gold"),f"upgrade_{kind}"))
            # Expose the nearest fragment gate without inventing a new cost.
            fragments = normalize_fragments(p.get(f"{kind}_fragments_json"), set(self.skill_catalog if kind=="skill" else self.companion_catalog))
            fragment_resource = f"{kind}_fragments"
            rank_gates=[]; awakening_gates=[]
            catalog = self.skill_catalog if kind=="skill" else self.companion_catalog
            for entity_id, entry in collection.items():
                if entity_id not in catalog or not entry.get("owned", True): continue
                state=normalize_rank_state(entry); rarity=str(catalog[entity_id].get("rarity","common")); balance=fragments.get(entity_id,0)
                cost=star_cost(rarity,state)
                if cost is not None and balance<cost: rank_gates.append((cost-balance,cost,balance,entity_id))
                acost=awakening_cost(rarity,state["awakening_tier"]) if state["rank"]>=MAX_RANK else None
                if acost is not None and balance<acost: awakening_gates.append((acost-balance,acost,balance,entity_id))
            if rank_gates and not data["best_rank"]:
                _,required,current,entity_id=min(rank_gates); blockers.append(blocker(fragment_resource,required,current,f"rank_{kind}",f"{entity_id}: не хватает {required-current} fragments"))
            if awakening_gates and not data["best_awakening"]:
                _,required,current,entity_id=min(awakening_gates); blockers.append(blocker(fragment_resource,required,current,f"awaken_{kind}",f"{entity_id}: не хватает {required-current} fragments"))
        if not chest["chest_upgrade_ready"] and chest["chest_upgrade_gold_cost"]:
            blockers.append(blocker("gold",chest["chest_upgrade_gold_cost"],chest["gold_current"],"upgrade_chest"))
        if not skill_tickets: blockers.append(blocker("skill_summon_scrolls",1,0,"summon_skill_ticket"))
        if not companion_tickets: blockers.append(blocker("companion_summon_contracts",1,0,"summon_companion_ticket"))
        flow = self._daily_flow(counters, daily_claim, weekly_claim, achievements, boss_available, can_battle, skills, companions)
        quick = self._quick_actions(counters, skills, companions, boss_available, can_battle)
        return {"server_time": now_i, "priority_action": priority_action,
                "recommended_actions": [r for r in recommendations[1:6]], "counters": counters, "blockers": blockers,
                "summaries": {"better_items": better, "skills": skills, "companions": companions,
                              "premium_crystals": max(0,_int(p.get("premium_crystals"))), "crystal_summon_available": max(0,_int(p.get("premium_crystals"))) >= SUMMON_COSTS[1]["premium_crystals"]},
                "notifications": {"total_count": sum(n["count"] for n in notifications),
                    "critical_count": sum(n["count"] for n in notifications if n["severity"]=="critical"), "items": notifications},
                "quick_actions": quick, "daily_flow": flow}

    def _daily_flow(self,c,daily,weekly,ach,boss,can_battle,skills,companions):
        specs=[("claim_offline","Офлайн-награда",bool(c["offline_claimable"]),not c["offline_claimable"],False,"offline",ROUTES["offline"]),
          ("claim_daily_quests","Ежедневные задания",daily>0,daily==0,False,"quests",ROUTES["quests"]),
          ("claim_weekly_quests","Еженедельные задания",weekly>0,weekly==0,True,"quests",ROUTES["quests"]),
          ("claim_achievements","Достижения",ach>0,ach==0,True,"quests",ROUTES["quests"]),
          ("upgrade_chest","Улучшить сундук",bool(c["chest_upgrade_ready"]),not c["chest_upgrade_ready"],True,"chest",ROUTES["chest"]),
          ("resolve_pending_loot","Решить предмет",bool(self.player.get("pending_loot")),not bool(self.player.get("pending_loot")),False,"equipment",ROUTES["pending_loot"]),
          ("upgrade_skills","Улучшить навыки",skills["affordable_count"]>0,skills["affordable_count"]==0,True,"skills",ROUTES["skill_upgrade"]),
          ("upgrade_companions","Улучшить спутников",companions["affordable_count"]>0,companions["affordable_count"]==0,True,"companions",ROUTES["companion_upgrade"]),
          ("summon","Призыв",c["skill_ticket_summons"]+c["companion_ticket_summons"]>0,c["skill_ticket_summons"]+c["companion_ticket_summons"]==0,True,"summon",ROUTES["summon_skill"]),
          ("rank_or_awaken","Ранг или пробуждение",sum(c[k] for k in ("rankable_skills","rankable_companions","awakenable_skills","awakenable_companions"))>0,False,True,"progression",ROUTES["skill_rank"]),
          ("defeat_boss","Победить босса",boss,not boss,True,"battle",ROUTES["boss"]),
          ("continue_stage","Продолжить этап",can_battle,False,False,"battle",ROUTES["battle"])]
        steps=[]
        for sid,title,ready,completed,optional,section,route in specs:
            status="ready" if ready else "optional" if optional else "completed" if completed else "blocked"
            steps.append({"step_id":sid,"title":title,"status":status,"action_available":ready,"target_section":section,
                          "target_route":route,"blocker_summary":None if ready or completed else "Действие сейчас недоступно",
                          "progress_current":1 if completed else 0,"progress_target":1})
        mandatory=[s for s in steps if s["status"]!="optional"]
        completed=sum(s["status"]=="completed" for s in mandatory)
        next_step=next((s for s in steps if s["status"]=="ready"),next((s for s in steps if s["status"]=="blocked"),None))
        return {"steps":steps,"completed_steps":completed,"next_step":next_step,
                "completion_percent":round(100*completed/max(1,len(mandatory)))}

    def _quick_actions(self,c,skills,companions,boss,can_battle):
        offline_version=max(1,_int(self.player.get("offline_claim_version"),1))
        skill_pity=normalize_summon_state(self.player.get("skill_summon_state_json"))["pity_version"]
        companion_pity=normalize_summon_state(self.player.get("companion_summon_state_json"))["pity_version"]
        definitions=[("claim_offline","Забрать офлайн",c["offline_claimable"],ROUTES["offline"],{"client_action_id":"<uuid>","expected_claim_version":offline_version},False),
          ("claim_all_daily","Забрать дневные",c["claimable_quests"]>0,ROUTES["quests"],{"quest_type":"daily","client_action_id":"<uuid>"},False),
          ("claim_all_weekly","Забрать недельные",c["claimable_quests"]>0,ROUTES["quests"],{"quest_type":"weekly","client_action_id":"<uuid>"},False),
          ("claim_all_achievements","Забрать достижения",c["claimable_achievements"]>0,ROUTES["quests"],{"quest_type":"achievement","client_action_id":"<uuid>"},False),
          ("upgrade_chest","Улучшить сундук",c["chest_upgrade_ready"],ROUTES["chest"],{"client_action_id":"<uuid>"},True),
          ("open_chest","Открыть сундук",_int(self.player.get("chests"))>0 and not bool(self.player.get("pending_loot")),"/loot/open",{},True),
          ("upgrade_best_skill","Улучшить навык",bool(skills["best_upgrade"]),ROUTES["skill_upgrade"],{"skill_id":skills["best_upgrade"]["entity_id"] if skills["best_upgrade"] else None,"expected_level":skills["best_upgrade"]["level"] if skills["best_upgrade"] else None,"client_action_id":"<uuid>"},True),
          ("upgrade_best_companion","Улучшить спутника",bool(companions["best_upgrade"]),ROUTES["companion_upgrade"],{"companion_id":companions["best_upgrade"]["entity_id"] if companions["best_upgrade"] else None,"expected_level":companions["best_upgrade"]["level"] if companions["best_upgrade"] else None,"client_action_id":"<uuid>"},True),
          ("rank_best_skill","Повысить ранг навыка",bool(skills["best_rank"]),ROUTES["skill_rank"],{"entity_id":skills["best_rank"]["entity_id"] if skills["best_rank"] else None,"expected_rank_version":skills["best_rank"]["rank_version"] if skills["best_rank"] else None,"client_action_id":"<uuid>"},True),
          ("rank_best_companion","Повысить ранг спутника",bool(companions["best_rank"]),ROUTES["companion_rank"],{"entity_id":companions["best_rank"]["entity_id"] if companions["best_rank"] else None,"expected_rank_version":companions["best_rank"]["rank_version"] if companions["best_rank"] else None,"client_action_id":"<uuid>"},True),
          ("summon_skill_ticket","Призвать навык",c["skill_ticket_summons"]>0,ROUTES["summon_skill"],{"count":1,"payment_type":"ticket","expected_pity_version":skill_pity,"client_action_id":"<uuid>"},True),
          ("summon_companion_ticket","Призвать спутника",c["companion_ticket_summons"]>0,ROUTES["summon_companion"],{"count":1,"payment_type":"ticket","expected_pity_version":companion_pity,"client_action_id":"<uuid>"},True),
          ("continue_battle","Продолжить бой",can_battle,ROUTES["battle"],{},False)]
        return [{"action_id":a,"label":label,"available":bool(avail),"target_route":route,"payload_template":payload,
                 "confirmation_required":confirm,"blocker":None if avail else "Действие сейчас недоступно"}
                for a,label,avail,route,payload,confirm in definitions]


def build_growth_center(player: Mapping[str, Any], **context: Any) -> dict:
    now = float(context.pop("now", time.time())); player_id = _int(player.get("telegram_id"))
    cached = _cached(player_id, now)
    if cached is not None:
        return cached
    growth = GrowthCenter(player, now=now, **context).build()
    response = {"growth_center": growth, "refreshed_at": int(now), "cache_ttl_seconds": CACHE_TTL_SECONDS}
    _put(player_id, now, response)
    return response


def compact_summary(growth: Mapping[str, Any]) -> dict:
    counters=growth["counters"]; notifications=growth["notifications"]; flow=growth["daily_flow"]
    return {"priority_action":growth["priority_action"],"total_notifications":notifications["total_count"],
            "critical_notifications":notifications["critical_count"],
            "claimable_rewards":counters["claimable_quests"]+counters["claimable_milestones"]+counters["claimable_achievements"]+int(counters["offline_claimable"]),
            "available_upgrades":sum(counters[k] for k in ("affordable_skill_upgrades","affordable_companion_upgrades","rankable_skills","rankable_companions","awakenable_skills","awakenable_companions")),
            "inventory_warning":None,
            "next_daily_step":flow["next_step"]}
