import re
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


class ContractParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.id_occurrences = []
        self.screens = []
        self.icon_buttons_without_label = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
            self.id_occurrences.append(attrs["id"])
        if "data-main-screen" in attrs:
            self.screens.append(attrs["data-main-screen"])
        classes = set(attrs.get("class", "").split())
        if tag == "button" and "icon-button" in classes and not attrs.get("aria-label"):
            self.icon_buttons_without_label.append(attrs.get("id", "<anonymous>"))


class FrontendUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = ContractParser()
        cls.parser.feed(HTML)

    def test_five_sections_exist_without_home(self):
        self.assertEqual(
            {"battle", "equipment", "progression", "summon", "quests"},
            set(self.parser.screens),
        )
        self.assertNotIn('data-open-screen="home"', HTML)
        self.assertNotIn('data-main-screen="home"', HTML)

    def test_battle_is_permanent_start_screen(self):
        self.assertIn('activeScreen: "battle"', HTML)
        self.assertIn('frontendState.activeScreen = "battle"', HTML)
        self.assertIn('openMainScreen("battle");', HTML)
        self.assertNotIn('activeScreen: "home"', HTML)
        nav = re.search(r'<nav aria-label="Основная навигация".*?</nav>', HTML, re.S)
        self.assertIsNotNone(nav)
        self.assertEqual(5, nav.group(0).count('data-open-screen="'))
        self.assertEqual(5, nav.group(0).count('<svg '))

    def test_badges_are_server_driven(self):
        self.assertIn("function setNavBadge", HTML)
        self.assertIn('setNavBadge("quests", root.counters?.claimable_quests', HTML)
        self.assertIn('setNavBadge("equipment", player.pending_loot', HTML)

    def test_growth_center_is_embedded_and_recommendations_are_bounded(self):
        self.assertIn('id="battleGrowthPriority"', HTML)
        self.assertIn('id="battleGrowthOpen"', HTML)
        self.assertNotIn('id="homePriority"', HTML)
        self.assertIn('.filter(action => action.target_route !== priority.target_route).slice(0, 4)', HTML)

    def test_battle_hud_contract(self):
        for element_id in (
            "heroHpBar", "enemyHpBar", "stageProgress", "battleCompanions",
            "battleChestAction", "battlePendingLoot", "skill-dock",
        ):
            if element_id == "skill-dock":
                self.assertIn('class="skill-dock"', HTML)
            else:
                self.assertIn(element_id, self.parser.ids)
        for marker in ("enemyStatusEffects", "enemyDamageProfile", "eliteBadge", "damageNumber"):
            self.assertIn(marker, self.parser.ids)

    def test_secondary_systems_open_over_battle(self):
        self.assertIn('frontendState.activeScreen = "battle"', HTML)
        self.assertIn('if (name === "equipment") $("lootButton").click()', HTML)
        self.assertIn('if (name === "quests") elements.questsBattleButton.click()', HTML)

    def test_quests_have_no_extra_open_step(self):
        self.assertNotIn("Открыть список заданий", HTML)
        self.assertIn("renderDailyQuests();", HTML)
        for label in ("Ежедневные", "Недельные", "Достижения"):
            self.assertIn(label, HTML)

    def test_resource_drawer_and_modal_contracts(self):
        for element_id in ("resourceDrawer", "resourceDrawerOpen", "resourceDrawerClose", "confirmDialog", "confirmAccept", "toastRegion"):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn('event.key !== "Escape"', HTML)

    def test_dangerous_actions_use_confirmation(self):
        for title in (
            "Разобрать предмет?",
            "Массовый разбор?",
            "Переработать характеристику?",
            "Повысить несколько рангов?",
            "Пробудить сущность?",
        ):
            self.assertIn(f'confirmAction("{title}"', HTML)

    def test_ticket_summon_is_fast_and_crystal_summon_confirms(self):
        self.assertIn('button.dataset.summonPayment === "premium_crystals"', HTML)
        self.assertIn('confirmAction("Потратить кристаллы?"', HTML)
        self.assertRegex(HTML, r'else\s*\{\s*executeSummon\(\);\s*\}')

    def test_loading_empty_error_states_exist(self):
        for css_class in ("loading-overlay", "skeleton", "empty-state", "error-state"):
            self.assertIn(css_class, HTML)
        self.assertIn("api-error-indicator", HTML)

    def test_no_static_horizontal_overflow_and_mobile_safe_area(self):
        self.assertIn("overflow-x:hidden", HTML)
        self.assertIn("safe-area-inset-bottom", HTML)
        self.assertIn("100dvh", HTML)
        self.assertIn("min-height:44px", HTML)
        self.assertIn("safe-area-inset-top", HTML)
        self.assertIn("padding-bottom:calc(176px + env(safe-area-inset-bottom))", HTML)

    def test_required_icon_button_labels(self):
        self.assertEqual([], self.parser.icon_buttons_without_label)
        for label in ("Открыть все ресурсы", "Открыть настройки", "Закрыть ресурсы"):
            self.assertIn(f'aria-label="{label}"', HTML)

    def test_existing_api_routes_remain_present(self):
        routes = (
            "/player", "/boss/start", "/loot/open", "/inventory/salvage",
            "/inventory/reroll-secondary", "/chest/upgrade", "/quests/claim",
            "/offline/status", "/offline/claim", "/summon/status",
            "/summon/skill", "/summon/companion",
        )
        for route in routes:
            self.assertIn(route, HTML)

    def test_javascript_syntax(self):
        scripts = re.findall(r"<script>(.*?)</script>", HTML, flags=re.S)
        self.assertEqual(1, len(scripts))
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(scripts[0])
            handle.flush()
            result = subprocess.run(
                ["node", "--check", handle.name], capture_output=True, text=True
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_approved_battle_shell_is_the_visible_primary_shell(self):
        self.assertIn('id="approvedBattleShell"', HTML)
        self.assertIn('class="approved-game"', HTML)
        self.assertIn('.app>.top-hud,.app>.shell-screen,.app>#battlefield,.app>#shellNavigation{display:none!important}', HTML)
        self.assertIn('document.body.dataset.activeScreen = "battle"', HTML)
        self.assertNotIn('data-approved-nav="home"', HTML)

    def test_approved_navigation_has_exactly_five_entries(self):
        nav = re.search(r'<nav class="approved-nav".*?</nav>', HTML, re.S)
        self.assertIsNotNone(nav)
        self.assertEqual(5, nav.group(0).count('data-approved-nav="'))
        self.assertEqual(
            ["hero", "trials", "battle", "shop", "more"],
            re.findall(r'data-approved-nav="([^"]+)"', nav.group(0)),
        )
        self.assertIn('data-approved-nav="battle" type="button" aria-current="page"', nav.group(0))

    def test_approved_hud_and_battle_have_server_targets(self):
        for element_id in (
            "approvedPlayerName", "approvedPlayerLevel", "approvedPower",
            "approvedHeroHp", "approvedHeroMaxHp", "approvedHeroHpBar",
            "approvedGold", "approvedCrystals", "approvedEnemyName",
            "approvedEnemyHp", "approvedEnemyMaxHp", "approvedEnemyHpBar",
            "approvedStageMeta", "approvedHeroSprite", "approvedEnemySprite",
            "approvedDamage", "approvedCombatMessage",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn('player?.progression_resources?.premium_crystals ?? player.gems', HTML)
        self.assertIn('player.kills_in_stage ?? player.wave', HTML)
        self.assertIn('player.enemy_archetype', HTML)

    def test_battle_auto_controls_are_not_rendered(self):
        enemy_header = re.search(r'<section class="approved-stage">.*?</section>', HTML, re.S)
        skill_renderer = HTML.split("function renderApprovedSkills()", 1)[1].split("function renderApprovedEquipment", 1)[0]
        self.assertIsNotNone(enemy_header)
        self.assertNotIn("AUTO", enemy_header.group(0))
        self.assertNotIn("approvedAuto", enemy_header.group(0))
        self.assertNotIn("AUTO", skill_renderer)
        self.assertNotIn('data-approved-action="skills-auto"', skill_renderer)

    def test_chest_auto_open_is_distinct_and_keeps_its_handler(self):
        self.assertRegex(
            HTML,
            r'id="approvedChestAuto"[^>]*aria-label="Автооткрытие сундука"',
        )
        self.assertIn('$("approvedChestAuto").addEventListener("click", (event) => { event.stopPropagation(); toggleAuto(); })', HTML)

    def test_enemy_hp_and_localized_profile_are_server_driven(self):
        for element_id in ("approvedEnemyHp", "approvedEnemyMaxHp", "approvedEnemyHpBar"):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn('setText("approvedEnemyHp", formatNumber(player.enemy_hp || 0))', HTML)
        self.assertIn('setText("approvedEnemyMaxHp", formatNumber(player.enemy_max_hp || 0))', HTML)
        self.assertIn('approvedPercent(player.enemy_hp, player.enemy_max_hp)', HTML)
        for localized in (
            'brute:"Громила"', 'mystic:"Мистик"', 'toxic:"Ядовитый"',
            'guardian:"Страж"',
        ):
            self.assertIn(localized, HTML)

    def test_all_damage_types_have_one_user_facing_mapping(self):
        mapping = HTML.split("const DAMAGE_TYPE_NAMES", 1)[1].split("});", 1)[0]
        for localized in (
            'physical:"физический урон"', 'nature:"природный урон"',
            'poison:"ядовитый урон"', 'arcane:"магический урон"',
            'true:"чистый урон"',
        ):
            self.assertIn(localized, mapping)
        self.assertIn("function damageTypeName(value)", HTML)
        self.assertIn("function localizeDamageTypes(value)", HTML)

    def test_combat_message_and_log_localize_internal_damage_ids(self):
        set_message = HTML.split("function setMessage(text, isError = false)", 1)[1].split("function showDamage", 1)[0]
        self.assertIn("text = localizeDamageTypes(text);", set_message)
        self.assertIn('$("approvedCombatMessage").textContent = text', set_message)
        self.assertIn("event.textContent = text", set_message)
        suffix = HTML.split("function combatEffectSuffix(result)", 1)[1].split("function stageSequenceMessage", 1)[0]
        self.assertIn('damageTypeName(event.damage_type || "physical")', suffix)
        self.assertNotIn('`${event.damage_type || "physical"}', suffix)

    def test_damage_type_localization_covers_visible_dynamic_details(self):
        for marker in (
            "damageTypeName(player.enemy_attack_type", "damageTypeName(weakType)",
            "damageTypeName(kind)", "localizeDamageTypes(companion.description)",
            "localizeDamageTypes(skill.description)",
            "localizeDamageTypes(skill.short_description",
            "localizeDamageTypes(node.effect_summary",
        ):
            self.assertIn(marker, HTML)

    def test_battle_scene_message_and_skill_dock_have_one_flow(self):
        shell = re.search(r'<main class="approved-game".*?</main>', HTML, re.S)
        self.assertIsNotNone(shell)
        markup = shell.group(0)
        battle_end = markup.index('</section>', markup.index('class="approved-battle"'))
        dock_start = markup.index('id="approvedSkillDock"')
        self.assertLess(battle_end, dock_start)
        self.assertEqual(1, markup.count('class="approved-message"'))
        self.assertEqual(1, markup.count('id="approvedCombatMessage"'))
        self.assertIn('.approved-dock{position:relative', HTML)
        self.assertNotRegex(HTML, r'\.approved-dock\{[^}]*position:(?:absolute|fixed)')
        self.assertIn('pointer-events:none', HTML.split('.approved-message{', 1)[1].split('}', 1)[0])

    def test_existing_battle_skill_handlers_are_preserved(self):
        self.assertIn('useBattleSkill(String(skill.dataset.approvedSkill || ""))', HTML)
        self.assertIn('skillCooldownRemaining(skillId, state)', HTML)
        self.assertIn('$("approvedSkillDock").addEventListener("click"', HTML)

    def test_approved_equipment_and_dock_are_dynamic(self):
        self.assertIn('id="approvedEquipmentGrid"', HTML)
        self.assertIn('Object.keys(SLOT_NAMES).map(slot =>', HTML)
        self.assertEqual(10, len(re.findall(r'\b(?:helmet|armor|gloves|pants|boots|weapon|necklace|ring|belt|talisman):"', HTML.split("const APPROVED_SLOT_ICONS", 1)[1].split("};", 1)[0])))
        self.assertIn('function renderApprovedSkills()', HTML)
        self.assertIn('skillCooldownRemaining(skillId, state)', HTML)
        self.assertIn('useBattleSkill(String(skill.dataset.approvedSkill', HTML)
        self.assertIn('player.companion_system', HTML)

    def test_approved_chest_inventory_pending_and_ticker_contract(self):
        for element_id in (
            "approvedChestOpen", "approvedChestAuto", "approvedPendingBadge",
            "approvedTicker", "approvedTickerCopy",
        ):
            self.assertIn(element_id, self.parser.ids)
        self.assertNotIn("approvedInventory", self.parser.ids)
        self.assertIn('$("approvedChestOpen").addEventListener("click", handleChestPrimaryAction)', HTML)
        self.assertIn('$("approvedChestAuto").addEventListener("click", (event) => { event.stopPropagation(); toggleAuto(); })', HTML)
        self.assertIn('const critical = (root?.notifications?.items || []).find(item => item.severity === "critical")', HTML)
        self.assertIn('root?.counters?.claimable_quests', HTML)
        self.assertIn('player?.offline_progression', HTML)
        self.assertIn('loot_progression?.chest_upgrade_ready', HTML)

    def test_pending_loot_copy_is_localized(self):
        pending = HTML.split("function showPendingLoot(item", 1)[1].split("function showLootModal", 1)[0]
        for label in ("Скорость атаки", "Шанс критического удара", "Критический урон", "Урон навыков", "Урон спутников", "Урон боссам", "Бонус лечения", "Контратака"):
            self.assertIn(label, HTML)
        for english in ("Secondary stats", "Raw power", "Build score"):
            self.assertNotIn(english, pending)
        self.assertIn("RARITY_NAMES[item.rarity]", pending)
        self.assertIn("SLOT_NAMES[item.slot]", pending)

    def test_approved_sheets_keep_existing_feature_handlers(self):
        for marker in (
            '$("lootButton").click()', 'openEnhancementsScreen()',
            'elements.bossButton.click()', '$("summonOpen").click()',
            'elements.questsBattleButton.click()', '$("offlineOpen").click()',
            'refreshGrowthCenter()', '$("resourceDrawerOpen").click()',
        ):
            self.assertIn(marker, HTML)
        self.assertIn('confirmAction("Потратить кристаллы?"', HTML)
        self.assertIn('confirmAction("Разобрать предмет?"', HTML)
        self.assertIn('confirmAction("Переработать характеристику?"', HTML)
        self.assertIn('"hero-awakening":"Ранг и пробуждение"', HTML)

    def test_mobile_skills_have_distinct_list_and_detail_states(self):
        self.assertIn('approvedSkillView = "list"', HTML)
        self.assertIn('approvedSkillView = "detail"', HTML)
        self.assertIn("function renderApprovedSkillList()", HTML)
        self.assertIn("function renderApprovedSkillDetail", HTML)
        self.assertIn('data-approved-skill-card=', HTML)
        self.assertIn('sheetNavigationStack.push({mode:"skill-detail"', HTML)
        list_renderer = HTML.split("function renderApprovedSkillList()", 1)[1].split("function renderApprovedSkillDetail", 1)[0]
        self.assertNotIn('data-approved-skill-action=', list_renderer)

    def test_mobile_skills_sheet_layout_contract(self):
        self.assertIn("max-height:88dvh", HTML)
        self.assertIn(".approved-sheet-scroll{min-height:0;flex:1 1 auto", HTML)
        self.assertIn("overflow-y:auto", HTML)
        self.assertIn(".approved-sheet-tabs{display:flex", HTML)
        self.assertIn("overflow-x:auto", HTML)
        self.assertIn("flex:0 0 auto", HTML)
        self.assertIn(".approved-sheet-scroll.has-skill-actions{padding-bottom:24px}", HTML)
        self.assertIn("grid-template-columns:repeat(auto-fit,minmax(142px,1fr))", HTML)

    def test_mobile_skill_actions_are_two_per_row(self):
        self.assertIn('.approved-skill-action-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))', HTML)
        self.assertIn("min-height:44px", HTML)
        detail_renderer = HTML.split("function renderApprovedSkillDetail", 1)[1].split("function openApprovedSkillSlotModal", 1)[0]
        rows = re.findall(r'<div class=\\?"approved-skill-action-row\\?">(.*?)</div>', detail_renderer)
        self.assertTrue(rows)
        self.assertTrue(all(row.count("data-approved-skill-action") <= 2 for row in rows))

    def test_mobile_skill_slot_modal_and_routes_are_preserved(self):
        for element_id in ("approvedSkillSlotModal", "approvedSkillSlotOptions", "approvedSkillSlotCancel"):
            self.assertIn(element_id, self.parser.ids)
        self.assertIn('confirmAction("Заменить навык?"', HTML)
        for marker in (
            '`/${plural}/upgrade${levels > 1 ? "-bulk" : ""}`',
            "/skills/equip?slot=", "/skills/unequip?slot=",
            '`/${kind === "skill" ? "skills" : "companions"}/rank-up${steps > 1 ? "-bulk" : ""}`',
            '`/${kind === "skill" ? "skills" : "companions"}/awaken`',
        ):
            self.assertIn(marker, HTML)

    def test_progression_mutations_use_server_state_and_release_mutex(self):
        renderer = HTML.split("async function upgradeProgression", 1)[1].split("function bindRankActions", 1)[0]
        self.assertIn("expected_level: expectedLevel", renderer)
        self.assertIn("player = result", renderer)
        self.assertNotIn('apiRequest("/player")', renderer)
        self.assertIn("progressionMutations.has(mutationKey)", renderer)
        self.assertIn("finally { progressionMutations.delete(mutationKey)", renderer)

    def test_companions_have_separate_mobile_list_detail_and_slot_flow(self):
        for marker in (
            "function renderApprovedCompanionList()",
            "function renderApprovedCompanionDetail",
            "data-approved-companion-card=",
            "data-approved-companion-action=",
            "function openApprovedCompanionSlotModal",
            'confirmAction("Заменить спутника?"',
        ):
            self.assertIn(marker, HTML)

    def test_progression_max_level_is_compared_not_used_as_boolean(self):
        for renderer_name in ("renderApprovedSkillDetail", "renderApprovedCompanionDetail"):
            renderer = HTML.split(f"function {renderer_name}", 1)[1].split("function ", 1)[0]
            self.assertIn("Number(rawEntry.level || 1) >= Number(rawEntry.max_level || 50)", renderer)
            self.assertNotIn("entry:rawEntry, slotIndex", renderer.split("const entry =", 1)[1])

    def test_cooldown_uses_server_remaining_with_monotonic_elapsed_time(self):
        renderer = HTML.split("function skillCooldownRemaining", 1)[1].split("function renderSkills", 1)[0]
        self.assertIn("state?.cooldown_remaining", renderer)
        self.assertIn("performance.now()", renderer)
        self.assertIn("player?._client_received_at", renderer)

    def test_no_duplicate_ids(self):
        self.assertEqual(len(self.parser.id_occurrences), len(set(self.parser.id_occurrences)))

    def test_compact_number_formatter_uses_approved_suffixes(self):
        self.assertIn('[[1e12,"T"],[1e9,"B"],[1e6,"M"],[1e3,"K"]]', HTML)
        self.assertIn('.replace(".", ",")', HTML)

    def test_api_errors_preserve_server_detail_and_recover_from_timeout(self):
        self.assertIn("function apiErrorDetail(detail", HTML)
        self.assertIn("const detail = apiErrorDetail(data.detail)", HTML)
        self.assertIn('error?.name === "AbortError"', HTML)
        self.assertIn("Нет связи с сервером", HTML)
        helper = HTML.split("function apiErrorDetail", 1)[1].split("async function apiRequest", 1)[0]
        self.assertIn("Array.isArray(detail)", helper)
        self.assertNotIn("[object Object]", helper)

    def test_growth_badge_ignores_legacy_inventory_capacity(self):
        renderer = HTML.split("function renderGrowthCenter()", 1)[1].split("async function refreshGrowthCenter", 1)[0]
        self.assertIn('setNavBadge("equipment", player?.pending_loot ? 1 : 0)', renderer)
        self.assertNotIn("inventory_free === 0", renderer)

    def test_bottom_buttons_route_to_five_unique_root_modes(self):
        mapping = HTML.split("const BOTTOM_ROOT_MODES", 1)[1].split(");", 1)[0]
        self.assertEqual(
            {"hero": "hero-home", "trials": "trials-home", "battle": "battle", "shop": "shop-home", "more": "more-home"},
            dict(re.findall(r'(hero|trials|battle|shop|more):"([^"]+)"', mapping)),
        )
        handler = HTML.split('$("approvedNavigation").addEventListener', 1)[1].split("});", 1)[0]
        self.assertIn("navigateBottomSection(button.dataset.approvedNav)", handler)

    def test_root_renderers_are_isolated_by_contract(self):
        boundaries = [
            ("renderHeroHome", "renderTrialsHome", ("hero-equipment", "hero-skills"), ("trial-chest-boss", "shop-skills", "more-quests")),
            ("renderTrialsHome", "renderShopHome", ("trial-chest-boss", "trial-gem-boss"), ("hero-skills", "shop-skills", "more-quests")),
            ("renderShopHome", "renderMoreHome", ("shop-skills", "shop-companions", "shop-history"), ("hero-skills", "trial-chest-boss", "more-quests")),
            ("renderMoreHome", "renderHeroEquipment", ("more-quests", "more-offline", "more-growth"), ("hero-skills", "trial-chest-boss", "shop-skills")),
        ]
        for start, end, included, excluded in boundaries:
            renderer = HTML.split(f"function {start}()", 1)[1].split(f"function {end}", 1)[0]
            for marker in included:
                self.assertIn(marker, renderer)
            for marker in excluded:
                self.assertNotIn(marker, renderer)

    def test_legacy_shared_hero_tabs_are_removed(self):
        self.assertNotIn('id="approvedHeroTabs"', HTML)
        self.assertNotIn("data-approved-hero-tab", HTML)
        self.assertNotIn("approvedSheetActions", HTML)

    def test_battle_closes_ui_without_fetching_player(self):
        router = HTML.split("function navigateBottomSection(section)", 1)[1].split("function restoreAfterPendingLoot", 1)[0]
        for marker in ("closeIncompatibleOverlays()", "closeApprovedSheet()", 'activeSheetMode = null', 'openBattleScreen()'):
            self.assertIn(marker, router)
        self.assertNotIn("apiRequest", router)
        self.assertNotIn('"/player"', router)

    def test_sheet_back_uses_stack_and_close_returns_to_battle(self):
        back = HTML.split("function navigateSheetBack()", 1)[1].split("function navigateBottomSection", 1)[0]
        self.assertIn("sheetNavigationStack.pop()", back)
        self.assertIn("renderSheetMode(sheetNavigationStack.at(-1).mode", back)
        self.assertIn('navigateBottomSection("battle")', HTML.split('$("approvedSheetClose").addEventListener', 1)[1].split(";", 1)[0])

    def test_pending_loot_preserves_and_restores_sheet_stack(self):
        pending = HTML.split("function showPendingLoot(item", 1)[1].split("function showLootModal", 1)[0]
        restore = HTML.split("function restoreAfterPendingLoot()", 1)[1].split("function updateBottomBadges", 1)[0]
        self.assertIn("pendingLootReturnState", pending)
        self.assertIn("sheetNavigationStack.map", pending)
        self.assertIn("sheetNavigationStack.splice", restore)
        self.assertIn("renderSheetMode(saved.mode", restore)

    def test_child_modes_have_expected_root_parent_in_stack(self):
        for mode in ("hero-equipment", "hero-skills", "hero-companions", "hero-stats", "hero-awakening"):
            self.assertIn(mode, HTML)
        for mode in ("more-quests", "more-offline", "more-growth", "more-leaderboard", "more-settings"):
            self.assertIn(mode, HTML)
        for mode in ("trial-chest-boss", "trial-gem-boss"):
            self.assertIn(mode, HTML)
        self.assertIn("sheetNavigationStack.push({mode", HTML)

    def test_active_section_and_aria_current_are_owned_by_router(self):
        state = HTML.split("function setBottomNavigationState(section)", 1)[1].split("function closeIncompatibleOverlays", 1)[0]
        self.assertIn("activeBottomSection = section", state)
        self.assertIn('setAttribute("aria-current", "page")', state)
        self.assertIn('removeAttribute("aria-current")', state)
        self.assertIn('classList.toggle("active", active)', state)

    def test_bottom_badges_use_independent_sources(self):
        badges = HTML.split("function updateBottomBadges()", 1)[1].split("function approvedTickerState", 1)[0]
        self.assertIn("upgrade_available", badges)
        self.assertIn("attempts_remaining", badges)
        self.assertIn("free_available", badges)
        self.assertIn("claimable_quests", badges)
        self.assertNotIn("battle:", badges)

    def test_mobile_bottom_section_contracts(self):
        self.assertIn('@media(max-width:359px){.section-home-grid.complex{grid-template-columns:1fr}', HTML)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", HTML)
        self.assertIn('.approved-nav [data-approved-nav="battle"]', HTML)
        self.assertIn("min-height:44px", HTML)
        self.assertIn("overflow-x:hidden", HTML)


if __name__ == "__main__":
    unittest.main()
