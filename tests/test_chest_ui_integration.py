import re
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.append(attrs["id"])


class ChestUiIntegrationTests(unittest.TestCase):
    def test_chest_surface_and_button_share_open_handler(self):
        self.assertIn('$("approvedChestInfo").addEventListener("click", (event) => { event.stopPropagation(); openChest(); })', HTML)
        self.assertIn('$("approvedChestOpen").addEventListener("click", handleChestPrimaryAction)', HTML)
        self.assertNotIn('$("approvedChestBar").addEventListener("click"', HTML)

    def test_chest_actions_are_isolated(self):
        self.assertIn('$("approvedChestUpgrade").addEventListener("click", (event) => { event.stopPropagation(); upgradeChest(); })', HTML)
        self.assertIn('$("approvedChestAuto").addEventListener("click", (event) => { event.stopPropagation(); toggleAuto(); })', HTML)
        self.assertNotIn('approvedChestOpen").addEventListener("click", upgradeChest', HTML)
        self.assertNotIn('approvedChestAuto").addEventListener("click", openChest', HTML)
        self.assertNotIn('approvedChestAuto").addEventListener("click", upgradeChest', HTML)
        for message in ("[chest] open requested", "[chest] upgrade requested", "[chest] auto toggle requested"):
            self.assertIn(f'console.debug("{message}"', HTML)

    def test_double_open_is_guarded(self):
        self.assertIn("if (chestOpenInProgress) return", HTML)
        self.assertIn("chestOpenInProgress = true", HTML)
        self.assertIn("chestOpenInProgress = false", HTML)

    def test_pending_sheet_has_server_data_and_actions(self):
        for marker in (
            "lootRarity", "lootMainStats", "lootSecondaryStats", "compareBuild",
            "equipLootButton", "sellLootButton", "lootDetailsButton",
        ):
            self.assertIn(f'id="{marker}"', HTML)
        for removed in ("keepLootButton", "lockLootButton", "salvageLootButton", "rerollLootButton"):
            self.assertNotIn(f'id="{removed}"', HTML)
        self.assertIn("function showPendingLoot(item", HTML)
        self.assertIn("showPendingLoot(pending, {comparison:result.pending_loot_comparison || result.comparison})", HTML)
        self.assertIn('resolveLoot("/loot/equip")', HTML)
        self.assertIn('confirmAction("Продать предмет?"', HTML)

    def test_inventory_full_is_absent_from_new_open_gate(self):
        open_function = HTML.split("async function openChest()", 1)[1].split("async function upgradeChest", 1)[0]
        render_shell = HTML.split("function renderApprovedBattleShell()", 1)[1].split("let enemyDefeating", 1)[0]
        self.assertNotIn("inventoryFull", open_function)
        self.assertNotIn("inventory_full", render_shell)
        self.assertNotIn('id="approvedInventory"', HTML)

    def test_auto_open_has_three_visible_states_and_mutexes(self):
        self.assertIn('aria-label="Автооткрытие сундука"', HTML)
        for label in ("Выкл.", "Работает", "Пауза", "Автосундук"):
            self.assertIn(label, HTML)
        self.assertIn("if (autoRequestInProgress", HTML)
        self.assertIn("if (autoToggleInProgress) return", HTML)
        self.assertIn("if (shouldContinue) scheduleAutoOpen()", HTML)
        self.assertIn("shouldContinue = !pending && !result.paused", HTML)
        self.assertIn('player.pending_loot ? "Найден предмет"', HTML)

    def test_legacy_bulk_salvage_protection_contract_is_preserved(self):
        self.assertIn("exclude_locked:true", HTML)
        self.assertIn("exclude_build_upgrades:true", HTML)

    def test_upgrade_badge_confirm_and_route(self):
        self.assertIn('id="approvedChestUpgradeBadge"', HTML)
        self.assertIn("chest_upgrade_ready", HTML)
        self.assertIn('confirmAction("Улучшить сундук?"', HTML)
        self.assertIn('apiRequest("/chest/upgrade", "POST"', HTML)
        self.assertIn("Недостаточно золота", HTML)
        self.assertIn("chest_upgrade_gold_cost", HTML)
        self.assertNotIn("Опыт сундука", HTML)

    def test_hero_experience_bar_is_server_driven(self):
        for marker in ("approvedHeroExp", "approvedHeroExpRequired", "approvedHeroExpBar"):
            self.assertIn(f'id="{marker}"', HTML)
        self.assertIn("player.hero_experience_current ?? player.level_exp?.current", HTML)
        self.assertIn("player.hero_experience_required ?? player.level_exp?.required", HTML)

    def test_upgrade_cost_cannot_overlay_open_button(self):
        self.assertIn(".approved-chest-upgrade-button{display:grid", HTML)
        self.assertIn(".approved-chest-upgrade{position:static", HTML)
        self.assertIn("pointer-events:none", HTML)

    def test_open_gate_does_not_depend_on_gold_or_upgrade(self):
        render_shell = HTML.split("function renderApprovedBattleShell()", 1)[1].split("let enemyDefeating", 1)[0]
        condition = re.search(r'\$\("approvedChestOpen"\)\.disabled\s*=\s*([^;]+)', render_shell).group(1)
        self.assertEqual("chestOpenInProgress || (!player.pending_loot && Number(player.chests || 0) <= 0)", condition.strip())
        self.assertNotIn("gold", condition)
        self.assertNotIn("upgrade", condition)
        self.assertFalse(eval("False or bool(None) or int(875) <= 0"))

    def test_pending_is_normalized_from_manual_and_auto_responses(self):
        self.assertIn("function pendingLootFrom(payload) { return payload?.pending_loot || payload?.item || null; }", HTML)
        self.assertEqual(2, HTML.count("const pending = pendingLootFrom(result)"))
        self.assertIn('console.debug("[loot] pending from open", pending)', HTML)
        self.assertIn('apiRequest("/loot/open", "POST"', HTML)
        self.assertIn('apiRequest("/loot/auto/open", "POST")', HTML)

    def test_player_reload_and_resume_restore_pending_sheet(self):
        render_player = HTML.split("function renderPlayer()", 1)[1].split("function setMessage", 1)[0]
        self.assertIn('console.debug("[loot] pending from player", player.pending_loot)', render_player)
        self.assertIn("if (player.pending_loot) showPendingLoot(player.pending_loot)", render_player)
        self.assertIn('apiRequest("/player").then((freshPlayer)', HTML)
        self.assertIn("showPendingLoot(player.pending_loot, {comparison:player.pending_loot_comparison, force:true})", HTML)

    def test_same_pending_item_is_not_reopened_on_combat_refresh(self):
        pending_function = HTML.split("function showPendingLoot(item", 1)[1].split("function showLootModal", 1)[0]
        self.assertIn("shownPendingLootId === itemId", pending_function)
        self.assertIn("shownPendingLootId = itemId", pending_function)
        self.assertNotIn("inventory", pending_function.lower())

    def test_closed_pending_can_be_reopened_from_chest_button(self):
        self.assertIn('$("approvedChestOpen").textContent = player.pending_loot ? "Решить" : "Открыть"', HTML)
        self.assertIn('Сначала наденьте или продайте найденную вещь', HTML)
        open_function = HTML.split("async function openChest()", 1)[1].split("async function upgradeChest", 1)[0]
        self.assertIn("showPendingLoot(player.pending_loot, {comparison:player.pending_loot_comparison, force:true})", open_function)
        self.assertIn('$("lootModalClose").addEventListener("click", () => { modalHide("lootOverlay")', HTML)

    def test_resolving_pending_resets_marker_and_resumes_auto(self):
        resolve_function = HTML.split("async function resolveLoot(path)", 1)[1].split("function sellCurrentLoot", 1)[0]
        self.assertIn("shownPendingLootId = null", resolve_function)
        self.assertIn('modalHide("lootOverlay")', resolve_function)
        self.assertIn("renderPlayer()", resolve_function)
        self.assertIn("scheduleAutoOpen(450)", resolve_function)

    def test_pending_uses_new_reusable_game_sheet(self):
        pending_function = HTML.split("function showPendingLoot(item", 1)[1].split("function showLootModal", 1)[0]
        self.assertIn('openGameSheet({mode:"pending-loot"', pending_function)
        self.assertIn('$("approvedSheetWrap")', pending_function)
        self.assertIn('$("approvedSheetContent")', pending_function)
        self.assertNotIn('modalShow("lootOverlay")', pending_function)
        for marker in ("approvedSheetWrap", "approvedSheetContent", "approvedSkillActions"):
            self.assertIn(f'id="{marker}"', HTML)

    def test_game_sheet_sets_visible_accessible_pending_state(self):
        game_sheet = HTML.split("function openGameSheet", 1)[1].split("function openApprovedSheet", 1)[0]
        self.assertIn('overlay.classList.add("open")', game_sheet)
        self.assertIn('overlay.setAttribute("aria-hidden", "false")', game_sheet)
        self.assertIn('sheet?.classList.add("open")', game_sheet)
        self.assertIn('activeSheetMode = mode', game_sheet)
        self.assertIn('document.body.classList.add("approved-sheet-open")', game_sheet)

    def test_pending_sheet_is_above_navigation_and_mobile_safe(self):
        self.assertIn(".approved-nav{position:fixed;z-index:180", HTML)
        self.assertIn(".approved-sheet-wrap{position:fixed;z-index:350;inset:0", HTML)
        self.assertIn(".approved-sheet.pending-loot-sheet{width:min(calc(100% - 16px),504px);height:auto;max-height:72dvh", HTML)
        self.assertIn("width:calc(100% - 8px);max-height:78dvh", HTML)
        pending_css = HTML.split(".approved-sheet.pending-loot-sheet{", 1)[1].split("}", 1)[0]
        self.assertNotIn("min-height", pending_css)
        self.assertIn("overflow-y:auto", HTML)
        self.assertIn("approved-pending-actions", HTML)

    def test_resolve_button_force_opens_without_opening_chest(self):
        handler = HTML.split("async function handleChestPrimaryAction", 1)[1].split("async function upgradeChest", 1)[0]
        pending_branch = handler.split('if ($("approvedChestOpen")', 1)[0]
        self.assertIn("event.stopPropagation()", handler)
        self.assertIn("force:true", pending_branch)
        self.assertNotIn("openChest()", pending_branch)
        self.assertIn('apiRequest("/player")', handler)

    def test_pending_sheet_content_and_actions(self):
        pending_function = HTML.split("function showPendingLoot(item", 1)[1].split("function showLootModal", 1)[0]
        for copy in ("Боевая мощь", "Урон", "Здоровье", "Сила предмета", "Цена продажи", "Оценка сборки", "Профиль сравнения"):
            self.assertIn(copy, pending_function)
        for action in ('data-pending-action="equip"', 'data-pending-action="sell"', 'data-pending-action="details"'):
            self.assertIn(action, pending_function)
        self.assertNotIn("Secondary stats", pending_function)
        self.assertNotIn("Raw power", pending_function)
        self.assertNotIn("Build score", pending_function)
        self.assertIn('const actions = `<div class="approved-skill-action-row">', pending_function)
        self.assertEqual(2, pending_function.split("const actions = `", 1)[1].split("`;", 1)[0].count("<button"))
        self.assertIn('resolveLoot("/loot/equip")', HTML)
        self.assertIn("sellCurrentLoot()", HTML)

    def test_pending_mode_hides_generic_hero_controls(self):
        game_sheet = HTML.split("function openGameSheet", 1)[1].split("function openApprovedSheet", 1)[0]
        self.assertNotIn('id="approvedHeroTabs"', HTML)
        self.assertNotIn("data-approved-hero-tab", HTML)
        self.assertIn('$("approvedSheetBack").hidden = true', game_sheet)
        self.assertIn('sheet?.classList.toggle("pending-loot-sheet", mode === "pending-loot")', game_sheet)
        self.assertIn('contentClass:"approved-pending-loot"', HTML)
        self.assertIn(".pending-loot-sheet .approved-sheet-back,.pending-loot-sheet .approved-sheet-tabs{display:none!important}", HTML)

    def test_pending_comparison_is_compact_row_list(self):
        self.assertIn("function pendingComparisonRow", HTML)
        self.assertIn("grid-template-columns:minmax(0,1fr) auto auto auto", HTML)
        self.assertIn("min-height:36px", HTML)
        pending_function = HTML.split("function showPendingLoot(item", 1)[1].split("function showLootModal", 1)[0]
        self.assertLess(pending_function.index('pendingComparisonRow("Боевая мощь"'), pending_function.index("secondaryPublic.forEach"))
        self.assertIn('class="approved-pending-delta ${tone}"', HTML)

    def test_pending_details_is_inline_accordion_and_sale_is_one_row(self):
        self.assertIn('class="approved-pending-sale"', HTML)
        self.assertIn('class="approved-pending-details-toggle"', HTML)
        self.assertIn('data-pending-details hidden', HTML)
        self.assertIn('button.setAttribute("aria-expanded", String(!details.hidden))', HTML)
        self.assertIn('.approved-pending-details[hidden]{display:none}', HTML)

    def test_pending_actions_are_sticky_and_do_not_overlay_content(self):
        self.assertIn(".approved-pending-actions{position:sticky", HTML)
        self.assertIn("min-height:50px", HTML)
        self.assertIn("padding:0 12px 8px", HTML)

    def test_auto_control_is_mobile_bounded(self):
        self.assertIn(".approved-chest-auto{display:grid", HTML)
        self.assertIn("width:70px;max-width:70px", HTML)
        self.assertIn("flex-shrink:0", HTML)
        self.assertIn("overflow:hidden", HTML)
        self.assertIn("@media(max-width:359px){.approved-chest{", HTML)
        self.assertIn('aria-label="Автооткрытие сундука"', HTML)
        self.assertNotIn("<b>Автосундук</b>", HTML)

    def test_routes_and_battle_auto_contract(self):
        for route in (
            "/loot/open", "/loot/equip", "/loot/sell", "/inventory/salvage",
            "/chest/upgrade", "/loot/auto/open", "/loot/auto/enable",
            "/loot/auto/disable",
        ):
            self.assertIn(route, HTML)
        approved_stage = re.search(r'<section class="approved-stage">.*?</section>', HTML, re.S)
        self.assertIsNotNone(approved_stage)
        self.assertNotIn("AUTO", approved_stage.group(0))

    def test_no_duplicate_ids_and_javascript_is_valid(self):
        parser = IdParser()
        parser.feed(HTML)
        ids = parser.ids
        self.assertEqual(len(ids), len(set(ids)))
        script = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(script)
            handle.flush()
            result = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
