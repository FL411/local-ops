from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def relative_luminance(color):
    channels = [int(color[index:index + 2], 16) / 255
                for index in (1, 3, 5)]
    channels = [
        value / 12.92 if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return (
        0.2126 * channels[0]
        + 0.7152 * channels[1]
        + 0.0722 * channels[2]
    )


def contrast_ratio(foreground, background):
    lighter, darker = sorted(
        (relative_luminance(foreground), relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def theme_block(source, selector):
    match = re.search(
        re.escape(selector) + r"\s*\{(?P<body>[^}]+)\}",
        source,
    )
    if not match:
        raise AssertionError(f"Missing theme block: {selector}")
    return match.group("body")


def theme_token_block(source, color_scheme):
    for match in re.finditer(
        r"(?P<selector>[^{}]+)\{(?P<body>[^{}]+)\}",
        source,
    ):
        body = match.group("body")
        if (
            re.search(rf"color-scheme:\s*{re.escape(color_scheme)}\s*;", body)
            and "--ink-4:" in body
        ):
            return body
    raise AssertionError(f"Missing {color_scheme} theme token block")


def css_variable(block, name):
    match = re.search(
        rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})",
        block,
    )
    if not match:
        raise AssertionError(f"Missing CSS color variable: --{name}")
    return match.group(1)


class FrontendStructureParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.tables = []
        self.buttons_inside_labels = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        active_table = next(
            (item["table"] for item in reversed(self.stack)
             if item["table"] is not None),
            None,
        )
        if attributes.get("role") == "table":
            active_table = {
                "label": attributes.get("aria-label"),
                "rows": 0,
                "headers": 0,
            }
            self.tables.append(active_table)
        if active_table is not None:
            if attributes.get("role") == "row":
                active_table["rows"] += 1
            if attributes.get("role") == "columnheader":
                active_table["headers"] += 1
        if tag == "button" and any(item["tag"] == "label" for item in self.stack):
            self.buttons_inside_labels.append(attributes.get("id", "anonymous"))
        self.stack.append({
            "tag": tag,
            "table": active_table if attributes.get("role") == "table" else None,
        })

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return


class FrontendAccessibilityContractTests(unittest.TestCase):
    def test_monitoring_tables_have_named_aria_structure(self):
        parser = FrontendStructureParser()
        parser.feed((ROOT / "static/index.html").read_text(encoding="utf-8"))

        self.assertEqual(
            [table["label"] for table in parser.tables],
            ["我的服务", "应用后台", "已隐藏服务", "关注的进程"],
        )
        self.assertEqual(
            [table["headers"] for table in parser.tables],
            [9, 9, 9, 5],
        )
        self.assertTrue(all(table["rows"] >= 1 for table in parser.tables))

    def test_form_labels_do_not_contain_buttons(self):
        parser = FrontendStructureParser()
        parser.feed((ROOT / "static/index.html").read_text(encoding="utf-8"))
        self.assertEqual(parser.buttons_inside_labels, [])

    def test_accessibility_and_narrow_screen_css_guards_exist(self):
        css = (ROOT / "static/base.css").read_text(encoding="utf-8")
        self.assertIn("@media (forced-colors: active)", css)
        self.assertIn(".app-grid { grid-template-columns: minmax(0, 1fr); }", css)
        self.assertIn(".tbl .tr.th > * { display: block !important; }", css)
        self.assertNotIn(".tbl .tr.th { display: none; }", css)

    def test_focus_indicators_avoid_hard_double_rings(self):
        base = (ROOT / "static/base.css").read_text(encoding="utf-8")

        self.assertIn(
            ".appearance-details summary:focus-visible .appearance-disclosure",
            base,
        )
        self.assertIn("text-decoration-thickness: 2px", base)
        self.assertNotIn(
            "box-shadow: var(--focus-ring) !important;\n}"
            "\n\n.appearance-details[open]",
            base,
        )

    def test_ops_hero_english_companion_uses_single_light_layer(self):
        ops = (ROOT / "static/themes/ops.css").read_text(encoding="utf-8")
        english = theme_block(ops, ".view-head h2::after")

        self.assertIn("content: 'LAUNCHPAD'", english)
        self.assertIn("display: inline-block", english)
        self.assertIn("font-family: var(--font-mono)", english)
        self.assertNotIn("color: transparent", english)
        self.assertNotIn("-webkit-text-stroke: 1", english)
        self.assertRegex(
            ops,
            r"(?s)@media \(max-width: 900px\)\s*\{.*?"
            r"\.view-head h2::after\s*\{[^}]*display:\s*block;",
        )

    def test_small_text_color_pairs_meet_wcag_aa(self):
        ops = (ROOT / "static/themes/ops.css").read_text(encoding="utf-8")
        ops_light = theme_token_block(ops, "light")
        ops_dark = theme_token_block(ops, "dark")

        pairs = [
            (css_variable(ops_light, "ink-4"),
             css_variable(ops_light, "room")),
            (css_variable(ops_dark, "ink-4"),
             css_variable(ops_dark, "card")),
            (css_variable(ops_light, "accent"),
             css_variable(ops_light, "card")),
            (css_variable(ops_dark, "accent"),
             css_variable(ops_dark, "card")),
            (css_variable(ops_light, "green"),
             css_variable(ops_light, "card")),
            (css_variable(ops_light, "red"),
             css_variable(ops_light, "card")),
            (css_variable(ops_dark, "red"),
             css_variable(ops_dark, "card")),
        ]
        for foreground, background in pairs:
            with self.subTest(foreground=foreground, background=background):
                self.assertGreaterEqual(
                    contrast_ratio(foreground, background),
                    4.5,
                )

    def test_linked_service_action_edits_instead_of_duplicating(self):
        source = (ROOT / "static/js/services.js").read_text(encoding="utf-8")
        self.assertIn("if (s.appId)", source)
        self.assertIn("if (linked) openAppModal(linked);", source)
        self.assertIn("linked ? 'pencil' : 'plus'", source)
        self.assertIn("svc.appId ? '编辑启动台应用' : '添加到启动台'", source)
        self.assertNotIn("configuredPortClaims", source)

    def test_adding_a_running_service_creates_and_attaches_in_one_flow(self):
        services = (ROOT / "static/js/services.js").read_text(encoding="utf-8")
        overlays = (ROOT / "static/js/overlays.js").read_text(encoding="utf-8")

        self.assertIn("attachPid: s.pid", services)
        self.assertIn("let pendingAttach = null", overlays)
        self.assertIn("保存并认领", overlays)
        self.assertIn("body.attachPid = attachRequest.pid", overlays)
        self.assertIn("app.attached", overlays)
        self.assertIn("willAttach && detectingProject", overlays)
        self.assertIn("已加入启动台并认领正在运行的进程", overlays)

    def test_task_outcomes_and_health_have_distinct_ui_contracts(self):
        core = (ROOT / "static/js/core.js").read_text(encoding="utf-8")
        launchpad = (ROOT / "static/js/launchpad.js").read_text(encoding="utf-8")
        overlays = (ROOT / "static/js/overlays.js").read_text(encoding="utf-8")

        self.assertIn("export function taskExitStatus", core)
        self.assertIn("lastExit.code === 130", core)
        for status in ("succeeded", "canceled", "failed", "stopped"):
            self.assertIn(f"'{status}'", core)
        self.assertIn("taskStatus === 'canceled' ? '已取消' : '已中止'", launchpad)
        self.assertIn("app.health && app.health.blocking", launchpad)
        self.assertIn("r.primary.disabled = blocked", launchpad)
        self.assertIn("配置与运行诊断", launchpad)
        self.assertIn("const isTask = modalKind === 'task'", overlays)
        self.assertIn("const stopVerb = isTask ? '中止任务' : '停止服务'", overlays)

    def test_mutations_use_fragment_bootstrapped_control_token(self):
        core = (ROOT / "static/js/core.js").read_text(encoding="utf-8")
        overlays = (ROOT / "static/js/overlays.js").read_text(encoding="utf-8")

        self.assertIn("console_token", core)
        self.assertIn("sessionStorage.setItem(CONTROL_TOKEN_STORAGE_KEY", core)
        self.assertIn("history.replaceState", core)
        self.assertIn("export function controlRequestHeaders", core)
        self.assertIn("'X-Console-Token'", core)
        self.assertIn("headers: controlRequestHeaders({ 'Content-Type': pendingIcon.type })",
                      overlays)

    def test_new_port_discovery_is_session_scoped_and_actionable(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        services = (ROOT / "static/js/services.js").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")

        self.assertIn('id="portDiscovery"', html)
        self.assertIn('id="portDiscoveryList" role="list"', html)
        self.assertIn('aria-live="polite"', html)
        self.assertIn("const discoverySeenKeys = new Set()", services)
        self.assertIn("let discoveryNeedsBaseline = true", services)
        self.assertIn("export function suspendPortDiscovery", services)
        self.assertIn("export function observePortDiscovery", services)
        self.assertIn("svc.instanceKey", services)
        self.assertIn("svc.group === 'mine'", services)
        self.assertIn("!svc.hidden", services)
        self.assertIn("!knownPorts.has(port)", services)
        self.assertIn("if (!app || !app.running) continue", services)
        self.assertIn("app.listening !== false", services)
        self.assertIn("discoveryItems.delete(key)", services)
        self.assertNotIn("present: false", services)
        for label in ("加入启动台", "忽略并隐藏", "暂时关闭"):
            self.assertIn(label, services)
        self.assertIn("observePortDiscovery(data)", app)
        self.assertIn("suspendPortDiscovery()", app)

    def test_port_conflict_dialog_offers_non_destructive_resolution(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        launchpad = (ROOT / "static/js/launchpad.js").read_text(encoding="utf-8")

        self.assertIn('id="diagOpen"', html)
        self.assertIn('id="diagEdit"', html)
        self.assertIn("打开占用服务", html)
        self.assertIn("修改当前卡片", html)
        self.assertIn("并不是由这张卡片启动的", launchpad)
        self.assertIn("两张卡片也可以保存相同端口", launchpad)
        self.assertIn("openAppModal(app)", launchpad)

    def test_create_actions_stay_in_launchpad_and_global_palette(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        launchpad_start = html.index('id="view-launchpad"')
        services_start = html.index('id="view-services"')
        self.assertIn('id="addSvcCard"', html[launchpad_start:services_start])
        self.assertIn('id="addTaskCard"', html[launchpad_start:services_start])
        self.assertNotIn('id="addSvcCard"', html[services_start:])
        self.assertNotIn('id="addTaskCard"', html[services_start:])
        self.assertIn("title: '添加服务'", app)
        self.assertIn("title: '添加批处理任务'", app)
        self.assertIn("row.tabIndex = -1", app)

    def test_launchpad_cards_have_keyboard_sorting_contract(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        source = (ROOT / "static/js/launchpad.js").read_text(encoding="utf-8")
        self.assertIn('id="reorderInstructions"', html)
        self.assertIn('id="reorderStatus"', html)
        self.assertIn("card.addEventListener('keydown', cardSortKeyDown)", source)
        self.assertIn("finishKeyboardSort(false)", source)
        self.assertIn("pointercancel', onCancel", source)
        self.assertNotIn("pointercancel', onUp", source)

    def test_optional_appearance_section_and_unified_brand_assets_exist(self):
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        overlays = (ROOT / "static/js/overlays.js").read_text(encoding="utf-8")
        css = (ROOT / "static/base.css").read_text(encoding="utf-8")
        self.assertIn('id="appearanceDetails"', html)
        self.assertIn('class="appearance-disclosure-closed">展开设置</span>', html)
        self.assertIn('class="appearance-disclosure-open">收起设置</span>', html)
        self.assertIn('id="appearanceChevron"', html)
        self.assertIn("icon('chevron-down', 16)", overlays)
        self.assertIn(".appearance-details[open] .appearance-chevron", css)
        self.assertIn("transform: rotate(180deg)", css)
        self.assertIn('/assets/brand-mark.png', html)
        self.assertIn('/assets/favicon-32.png', html)
        for name in (
            "brand-mark.png",
            "console-app-icon.png",
            "favicon-32.png",
            "favicon.ico",
            "apple-touch-icon.png",
        ):
            with self.subTest(name=name):
                self.assertTrue((ROOT / "static/assets" / name).is_file())


if __name__ == "__main__":
    unittest.main()
