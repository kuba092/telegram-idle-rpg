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
        self.screens = []
        self.icon_buttons_without_label = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
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

    def test_six_main_screens_exist(self):
        self.assertEqual(
            {"home", "battle", "equipment", "progression", "summon", "quests"},
            set(self.parser.screens),
        )

    def test_router_keeps_one_main_screen_active(self):
        self.assertIn('section.hidden = section.dataset.mainScreen !== name', HTML)
        self.assertIn('frontendState.activeScreen = name', HTML)
        nav = re.search(r'<nav aria-label="Основная навигация".*?</nav>', HTML, re.S)
        self.assertIsNotNone(nav)
        self.assertEqual(6, nav.group(0).count('data-open-screen="'))

    def test_badges_are_server_driven(self):
        self.assertIn("function setNavBadge", HTML)
        self.assertIn('setNavBadge("quests", root.counters?.claimable_quests', HTML)
        self.assertIn('setNavBadge("equipment", player.pending_loot', HTML)

    def test_priority_is_unique_and_recommendations_are_bounded(self):
        self.assertEqual(1, HTML.count('id="homePriority"'))
        self.assertIn('.filter(action => action.target_route !== priority.target_route).slice(0, 4)', HTML)

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


if __name__ == "__main__":
    unittest.main()
