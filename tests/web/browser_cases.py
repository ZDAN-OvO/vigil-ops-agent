"""通过独立同源服务在 Chrome 检查网页，不使用数据库或集群。"""

import json
import unittest

from fixture import CARD, LOCAL_PATH, SECRET, Fixture
from playwright.sync_api import expect, sync_playwright

PATH = "/sessions/alert-demo"
COLORS = {
    "dim": "rgb(126, 122, 114)",
    "muted": "rgb(164, 159, 150)",
    "gold": "rgb(204, 164, 96)",
    "ok": "rgb(110, 143, 107)",
    "warn": "rgb(217, 161, 60)",
    "danger": "rgb(200, 42, 69)",
}


class BrowserCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver = cls.enterClassContext(sync_playwright())
        cls.browser = driver.chromium.launch(channel="chrome", headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.start_fixture()
        self.new_page()

    def start_fixture(self):
        fixture = Fixture()
        fixture.start()
        self.addCleanup(fixture.stop)
        self.fixture = fixture

    def new_page(self, width=1440, height=900, *, motion="reduce"):
        self.context = self.browser.new_context(
            viewport={"width": width, "height": height}, reduced_motion=motion, timezone_id="UTC"
        )
        self.addCleanup(self.context.close)
        self.errors = []
        self.external = []
        self.context.on(
            "request",
            lambda req: (
                self.external.append(req.url)
                if not req.url.startswith(self.fixture.url + "/")
                else None
            ),
        )
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on(
            "console",
            lambda message: (
                self.errors.append(message.text)
                if message.type == "error"
                and (
                    "Content Security Policy" in message.text
                    or "Encountered two children" in message.text
                )
                else None
            ),
        )
        self.card = self.page.locator(f"#card-{CARD}")
        self.popup = self.page.locator(".event-popover")

    def open_card(self, anchor=CARD):
        self.page.goto(self.fixture.url + PATH + f"#card-{anchor}")
        expect(self.page.locator("#session-meta")).to_have_attribute(
            "data-state", "awaiting_approval"
        )
        if anchor == CARD:
            expect(self.card).to_have_class("card anchor-target")
            expect(self.card).to_be_focused()

    def assert_seq(self, seq):
        current = self.page.locator(".ledger-cell[aria-current=true]")
        expect(current).to_have_attribute("data-seq", str(seq))
        expect(current).to_be_in_viewport()

    def assert_no_errors(self, *, external=()):
        self.assertEqual(self.errors, [])
        self.assertEqual(self.external, list(external))

    def assert_state(self, key, label, note, color, *, countdown=False):
        expect(self.card).to_have_attribute("data-state", key)
        expect(self.card.locator(".card-state")).to_have_text(label)
        expect(self.card.locator(".card-state")).to_have_css("color", COLORS[color])
        expect(self.card.locator(".card-foot-line")).to_have_text(note)
        expect(self.card.locator(".card-foot-line")).to_have_css("color", COLORS[color])
        expect(self.card.locator(".card-action-bar")).to_be_visible(
            visible=key == "pending_approval"
        )
        expect(self.card.locator(".card-foot .stamp")).to_be_visible(visible=countdown)
        expect(self.card.locator(".card-foot .spinner")).to_be_visible(visible=key == "executing")
        expect(self.page.locator("#session-meta")).to_have_attribute(
            "data-state", "awaiting_approval"
        )
        self.assert_seq(len(self.fixture.events["alert-demo"]))

    def test_card_matrix(self):
        fixtures = [
            ("voided-before", "voided", "已作废", "未写入集群", "muted"),
            ("voided-after", "voided", "已作废", "未写入集群", "muted"),
            ("rejected", "rejected", "已拒绝", "未写入集群", "muted"),
            ("denied", "denied", "策略拒绝", "未写入集群", "muted"),
            ("expired", "expired", "已过期", "未写入集群", "danger"),
            ("approved", "approved", "已批准 · 待执行", "已批准 · 等待租约持有者执行", "gold"),
            ("intent", "intent", "准备写入", "已取得动作锁 · 尚未取得发送权", "gold"),
            ("executing", "executing", "执行中", "已取得发送权 · 结果待定", "gold"),
            ("verified", "verified", "已验证", "结果 · 入口探测 · 已恢复", "ok"),
            ("failed", "failed", "失败", "目标版本已变 · 目标状态见第 9 节", "danger"),
            ("unknown", "unknown", "不确定", "结果不确定 · 目标保持锁定，等待人工核实", "danger"),
        ]
        pending = ("pending_approval", "待审批", "等待人工批准 · 授权在批准时才生成", "dim")
        approved = ("approved", "已批准 · 待执行", "已批准 · 等待租约持有者执行", "gold")
        for branch, state, label, note, color in fixtures:
            with self.subTest(branch=branch):
                self.context.close()
                self.fixture.stop()
                self.start_fixture()
                self.new_page()
                self.open_card()
                self.assert_state(*pending)
                expect(self.card.locator(".card-state .flag")).to_have_css(
                    "background-color", COLORS["danger"]
                )
                if branch == "rejected":
                    self.fixture.delay_decision = True
                    self.card.get_by_role("button", name="拒绝").click()
                    expect(self.card.get_by_text("已提交 · 等待账本更新")).to_be_visible()
                    self.assert_state(*pending)
                    self.assertEqual(self.fixture.pending_decision, "rejected")
                    self.fixture.decision("rejected")
                elif branch in {"voided-before", "denied"}:
                    self.fixture.receipt(state)
                else:
                    self.card.get_by_role("button", name="批准").click()
                    self.assert_state(*approved, countdown=True)
                    if branch == "approved":
                        timer = self.card.locator(".card-foot .stamp")
                        before = timer.inner_text()
                        expect(timer).not_to_have_text(before, timeout=2500)
                        self.assertEqual(
                            self.fixture.posts,
                            [
                                (
                                    "cards",
                                    {"decision": "approved", "fingerprint": "fixture-fingerprint"},
                                )
                            ],
                        )
                    elif branch == "intent":
                        self.fixture.receipt("approved", "intent")
                    elif branch in {"voided-after", "expired"}:
                        self.fixture.receipt("approved", "intent")
                        self.assert_state(
                            "intent",
                            "准备写入",
                            "已取得动作锁 · 尚未取得发送权",
                            "gold",
                            countdown=True,
                        )
                        self.fixture.receipt(state, "voided")
                    elif branch == "executing":
                        self.fixture.receipt(
                            "executing", "executing", phase="send_granted", written=None
                        )
                    elif branch == "verified":
                        self.fixture.receipt(
                            "succeeded",
                            "verified",
                            phase="verification",
                            written=True,
                            verification={"method": "入口探测", "summary": "已恢复"},
                        )
                    elif branch == "failed":
                        self.fixture.receipt("failed", "voided", reason="目标版本已变")
                    elif branch == "unknown":
                        self.fixture.receipt(
                            "unknown", "unknown", phase="send_granted", written=None
                        )
                self.assert_state(
                    state,
                    label,
                    note,
                    color,
                    countdown=branch in {"approved", "intent", "executing"},
                )
                self.assert_no_errors()

    def test_layout_styles_and_motion(self):
        for width, height in ((1440, 900), (1280, 800)):
            with self.subTest(viewport=(width, height)):
                self.context.close()
                self.new_page(width, height, motion="no-preference")
                self.open_card()
                page = self.page
                page.evaluate("document.fonts.ready")
                monitor = page.locator(".monitoring")
                expect(page.locator(".topbar")).to_have_css("height", "48px")
                expect(page.locator(".sessions")).to_have_css("width", "240px")
                expect(monitor).to_have_css("width", "360px")
                expect(page.locator(".ledger")).to_have_css("height", "64px")
                expect(page.locator("body")).to_have_css("background-color", "rgb(11, 12, 14)")
                expect(page.locator("#session-title")).to_have_css("font-size", "30px")
                self.assertIn(
                    "Noto Serif Display",
                    page.locator("#session-title").evaluate(
                        "(node) => getComputedStyle(node).fontFamily"
                    ),
                )
                expect(self.card.locator(".card-title")).to_have_css("font-size", "28px")
                expect(self.card.locator(".card-title .mono")).to_have_css("font-size", "20px")
                expect(self.card.locator(".card-risk")).to_have_css("color", COLORS["warn"])
                expect(self.card.locator(".approve")).to_have_css(
                    "background-color", COLORS["gold"]
                )
                expect(self.card.locator(".change-table")).to_contain_text("将被移除")
                expect(self.card.locator(".change-table")).to_contain_text("app:r4")
                expect(self.card.locator(".change-table")).to_contain_text("app:r3")
                expect(self.card.locator(".change-table")).not_to_contain_text(SECRET)
                expect(self.card.locator(".card-body .fields").first).to_contain_text("当前 2 副本")
                expect(page.locator(".feed-entry").first).to_have_css("animation-duration", "0.36s")
                expect(page.locator(".observation .fold-body").first).to_have_css(
                    "transition-duration", "0.24s, 0.24s"
                )
                expect(page.locator("#question")).to_have_css(
                    "transition-duration", "0.18s, 0.18s, 0.18s, 0.18s"
                )
                expect(page.locator(".sessions .spinner")).to_have_css("animation-duration", "1.6s")
                spark = page.locator(".spark path")
                expect(spark).to_have_css("stroke", COLORS["danger"])
                self.assertEqual(spark.get_attribute("d").count("M"), 2)
                measured = spark.evaluate("""node => {
                  const svg = node.ownerSVGElement;
                  return [
                    svg.getBoundingClientRect().width, svg.getBoundingClientRect().height,
                    parseFloat(getComputedStyle(node).strokeWidth) * Math.abs(node.getScreenCTM().a)
                  ];
                }""")
                self.assertEqual(measured[:2], [60, 16])
                self.assertLessEqual(measured[2], 1.5)
                self.assert_no_overflow()
                for control in page.locator(".observation button").all():
                    control.click()
                expect(page.locator(".observation .fold-body.open")).to_have_count(6)
                self.assert_no_overflow()
                page.locator(".ledger-cell[aria-current=true]").hover()
                expect(self.popup).to_be_visible()
                expect(self.popup).to_have_css("backdrop-filter", "blur(12px) saturate(1.2)")
                self.assert_no_overflow()
                page.keyboard.press("Escape")
                page.get_by_role("button", name="收起值班面板").click()
                expect(monitor).to_have_css("width", "42px")
                self.assert_no_overflow()
                page.emulate_media(reduced_motion="reduce")
                self.assertEqual(
                    page.evaluate("""() => Array.from(document.querySelectorAll('*'))
                  .filter(n => getComputedStyle(n).transitionProperty !== 'none'
                    || getComputedStyle(n).animationName !== 'none').length"""),
                    0,
                )
                self.assert_no_errors()

    def assert_no_overflow(self):
        self.assertEqual(
            self.page.evaluate("""() => [
          document.documentElement, document.body, document.querySelector('.workspace'),
          ...document.querySelector('.workspace').children,
          document.querySelector('.conversation-scroll'), document.querySelector('.ledger-window')
        ].filter(node => node.scrollWidth > node.clientWidth).map(node => node.className)"""),
            [],
        )

    def test_ledger_evidence_delivery_paging_and_reconnect(self):
        self.fixture.duplicate_events = True
        self.open_card()
        expect(self.page.get_by_text("已送入 · 第 6 步")).to_be_visible()
        expect(self.page.get_by_text("旁路 · 未送入主线")).to_be_visible()
        expect(self.page.get_by_text("观测不可回查").filter(visible=True)).to_be_visible()
        self.card.get_by_role("button", name="E1", exact=True).click()
        expect(self.page.locator(".observation .fold-body.open")).to_have_count(1)

        root = self.page.locator("#ledger")
        first = self.page.locator(".ledger-cell[data-seq]").first
        original = int(first.get_attribute("data-seq"))
        root.focus()
        self.page.keyboard.press("ArrowLeft")
        expect(first).to_have_attribute("data-seq", str(original - 1))
        self.assert_seq(15)
        first.focus()
        expect(self.popup).to_be_visible()
        self.page.keyboard.press("Tab")
        expect(self.popup).to_be_focused()
        self.page.keyboard.press("Shift+Tab")
        expect(first).to_be_focused()
        self.page.keyboard.press("Tab")
        self.page.keyboard.press("Escape")
        expect(first).to_be_focused()
        expect(self.popup).to_be_hidden()
        self.page.locator("#session-title").hover()
        first.hover()
        expect(self.popup).to_be_visible()
        first_rect = first.bounding_box()
        pop_rect = self.popup.bounding_box()
        self.assertLessEqual(pop_rect["width"], 560)
        self.assertLessEqual(pop_rect["height"], 200)
        self.assertAlmostEqual(first_rect["x"], pop_rect["x"], delta=1)
        self.page.mouse.move(pop_rect["x"] + 10, pop_rect["y"] + 10)
        expect(self.popup).to_be_visible()
        seq = int(first.get_attribute("data-seq"))
        self.assertEqual(
            json.loads(self.popup.inner_text()), self.fixture.events["alert-demo"][seq - 1]
        )
        expect(self.popup).not_to_contain_text(SECRET)
        expect(self.popup).not_to_contain_text(LOCAL_PATH)
        root.focus()
        for _ in range(2):
            self.page.keyboard.press("ArrowLeft")
        observation = root.locator('.ledger-cell[data-seq="8"]')
        observation.focus()
        projected = self.fixture.events["alert-demo"][7]
        self.assertEqual(json.loads(self.popup.inner_text()), projected)
        self.assertEqual(projected["observation"]["token"], "[已遮盖]")
        self.assertEqual(projected["observation"]["source"], "[已遮盖]")
        response = self.page.request.get(
            self.fixture.url
            + PATH.replace("/sessions", "/api/sessions")
            + "/events?after_seq=0&limit=100"
        )
        for content in (
            response.text(),
            self.page.request.get(self.fixture.url + "/api/monitoring").text(),
            self.popup.inner_text(),
        ):
            self.assertNotIn(SECRET, content)
            self.assertNotIn(LOCAL_PATH, content)
        self.fixture.query_cursors.clear()

        self.fixture.reject_reads = True
        expect(self.page.locator("#connection")).to_have_text("连接中断 · 正在重试")
        self.fixture.push("message", role="assistant", text="断开期间新增的消息")
        for number in range(105):
            self.fixture.push("evidence", evidence_id=f"page-{number}", observation_id="obs-0")
        self.fixture.reject_reads = False
        expect(self.page.get_by_text("断开期间新增的消息")).to_have_count(1)
        self.assert_seq(121)
        expect(self.page.locator("#connection")).to_be_empty()
        self.assertIn(15, self.fixture.query_cursors)
        self.assertIn(115, self.fixture.query_cursors)
        self.assertNotIn(0, self.fixture.query_cursors)
        expect(self.page.locator("body")).not_to_contain_text(SECRET)
        expect(self.page.locator("body")).not_to_contain_text(LOCAL_PATH)
        self.assert_no_errors()

    def test_ledger_popup_aligns_at_viewport_edge_and_tabs_to_next_cell(self):
        self.open_card()
        page = self.page
        page.get_by_role("button", name="收起值班面板").click()
        current = page.locator(".ledger-cell[aria-current=true]")
        current.hover()
        expect(self.popup).to_be_visible()
        page.wait_for_function("""() => {
          const popup = document.querySelector('.event-popover')?.getBoundingClientRect();
          const trigger = document.querySelector('.ledger-cell[aria-current=true]');
          return popup && Math.abs(popup.right - trigger.getBoundingClientRect().right) <= 1;
        }""")
        page.keyboard.press("Escape")
        expect(current).to_be_focused()
        expect(self.popup).to_be_hidden()
        page.keyboard.press("Enter")
        expect(self.popup).to_be_focused()
        page.keyboard.press("Escape")
        expect(current).to_be_focused()

        page.locator("#session-title").hover()
        cells = page.locator(".ledger-cell[data-seq]")
        cells.first.focus()
        page.keyboard.press("Tab")
        expect(self.popup).to_be_focused()
        page.keyboard.press("Tab")
        expect(cells.nth(1)).to_be_focused()
        self.assert_no_errors()

    def test_monitoring_questions_links_and_missing_values(self):
        self.open_card()
        page = self.page
        expect(page.locator(".grafana")).to_have_count(0)
        expect(page.locator(".hero")).to_have_text("1个异常服务")
        expect(page.locator(".monitor-block h3")).to_have_text(
            [
                "正在触发的告警",
                "入口探测",
                "最近变更 · 24 小时",
                "异常服务",
                "Warning 事件 · 1 小时",
                "节点",
            ]
        )
        expect(page.locator('[data-block="services"]')).to_contain_text("84%")
        expect(page.locator('[data-block="changes"]')).to_contain_text("批准人 值班员")
        alert = page.get_by_role("link", name="处理会话 alert-demo · 待审批")
        expect(alert).to_have_attribute("href", PATH)
        fold = page.get_by_role("button", name="其余 1 个服务正常")
        fold.click()
        expect(
            page.locator('[data-block="services"]').get_by_text("checkoutservice")
        ).to_be_visible()
        expect(page.locator("#question")).to_have_value("")
        probe = page.locator('[data-block="probes"] button')
        probe.focus()
        expect(probe.get_by_text("↵ 提问")).to_be_visible()
        page.keyboard.press("Space")
        question = page.locator("#question")
        expect(question).to_be_focused()
        expect(question).to_have_value(
            "Online Boutique 的 https://store.invalid/cart 这一小时可用率是多少，"
            "最近探测失败的原因是什么？"
        )
        page.locator('[data-block="services"] .monitor-question').click()
        expect(question).to_have_value("adservice 近 1 小时为什么重启了 3 次，最近一次在 02:14？")
        self.assertEqual(self.fixture.posts, [])
        page.get_by_role("button", name="发送").click()
        expect(page.locator(".message").filter(has_text="为什么重启了 3 次")).to_have_count(1)
        expect(question).to_have_value("")
        self.assertEqual(self.fixture.posts[0][0], "messages")
        self.assertTrue(self.fixture.posts[0][1]["input_id"])

        self.fixture.snapshot["alerts"] = {
            "status": "error",
            "data": None,
            "missing": [],
            "failed": ["alertmanager"],
        }
        expect(page.locator('[data-block="alerts"]')).to_contain_text("数据未取到", timeout=8000)
        expect(page.locator(".hero")).to_have_text("1个异常服务")
        self.fixture.snapshot["abnormal_service_count"] = None
        self.fixture.snapshot["services"]["status"] = "partial"
        expect(page.locator(".hero strong")).to_have_text("—", timeout=8000)
        expect(page.locator(".hero strong")).to_have_css("color", COLORS["dim"])
        expect(page.locator('[data-block="services"]')).to_contain_text("adservice")
        self.fixture.snapshot["abnormal_service_count"] = 0
        self.fixture.snapshot["nodes"]["data"]["abnormal"] = []
        external = Fixture()
        external.start()
        self.addCleanup(external.stop)
        self.fixture.snapshot["grafana_links"] = {
            key: f"{external.url}/grafana-fixture"
            for key in ["alerts", "probes", "changes", "services", "warnings", "nodes"]
        }
        expect(page.locator(".hero")).to_have_text("0全部服务正常", timeout=8000)
        expect(page.locator(".hero strong")).to_have_css("color", COLORS["ok"])
        expect(page.locator('[data-block="nodes"] button')).to_have_count(0)
        expect(page.locator(".grafana")).to_have_count(5)
        self.assert_no_errors()
        with page.expect_popup() as popup:
            page.locator('[data-block="services"] .grafana').click()
        popup.value.wait_for_load_state()
        self.assertEqual(popup.value.url, external.url + "/grafana-fixture")
        self.assert_no_errors(external=[external.url + "/grafana-fixture"])

    def test_read_only_and_card_links(self):
        self.fixture.approval_enabled = False
        self.open_card()
        expect(self.card.get_by_role("button", name="批准")).to_be_disabled()
        expect(self.card.get_by_role("button", name="拒绝")).to_be_disabled()
        expect(self.page.get_by_role("button", name="关闭告警会话")).to_be_disabled()
        expect(self.page.get_by_role("button", name="接手恢复")).to_be_disabled()
        expect(self.card.get_by_text("只读 · 浏览器缺少批准凭证")).to_be_visible()
        self.page.locator("#question").fill("网页只读时的问题")
        expect(self.page.get_by_role("button", name="发送")).to_be_disabled()
        self.fixture.decision("approved")
        self.page.reload()
        expect(self.card.locator(".card-state")).to_have_text("已批准 · 待执行")
        expect(self.card).to_be_focused()
        self.fixture.receipt("voided", "voided")
        self.page.reload()
        expect(self.card.locator(".card-state")).to_have_text("已作废")
        expect(self.card.locator(".card-foot .stamp")).to_be_hidden()
        self.open_card("missing")
        expect(self.page.get_by_text("此会话中找不到链接指向的卡片")).to_be_visible()
        self.assertEqual(self.fixture.posts, [])
        self.assert_no_errors()

    def test_alert_navigation_resume_and_close(self):
        page = self.page
        page.goto(self.fixture.url)
        expect(page.locator("#session-list")).to_contain_text("接手恢复")
        expect(page.locator("#session-list .session-row")).to_have_count(5)
        page.get_by_role("link", name="处理会话 alert-demo · 待审批").click()
        expect(page).to_have_url(self.fixture.url + PATH)
        expect(page.locator(".message").first.locator(".stamp").first).to_have_text("ALERT")
        expect(page.locator("#question")).to_have_value("")
        page.get_by_role("button", name="关闭告警会话").click()
        expect(page.locator("#session-meta")).to_have_attribute("data-state", "closed")
        expect(self.card.locator(".card-state")).to_have_text("已作废")
        expect(page.get_by_role("button", name="发送")).to_be_disabled()
        page.get_by_role("button", name="接手恢复").click()
        expect(page).to_have_url(self.fixture.url + "/sessions/interrupted-demo")
        expect(page.locator("#session-meta")).to_have_attribute("data-state", "working")
        self.assert_no_errors()

    def test_round_failure_and_budget_status_are_visible(self):
        self.fixture.push("result", status="failed", summary="模型调用失败")
        self.fixture.push("message", role="assistant", text="已取得初步观测")
        self.fixture.push("result", status="budget_exhausted", summary="已取得初步观测")
        self.open_card()
        expect(self.page.get_by_text("运行失败", exact=True)).to_be_visible()
        expect(self.page.get_by_text("模型调用失败", exact=True)).to_be_visible()
        expect(self.page.get_by_text("预算已用完", exact=True)).to_be_visible()
        expect(self.page.get_by_text("已取得初步观测", exact=True)).to_have_count(1)
        self.assert_no_errors()

    def test_pod_card_uses_target_fields(self):
        event = self.fixture.events["alert-demo"][-1]
        event["action"] = "restart_pod"
        event["target"].update(kind="Pod", name="frontend-fixture", uid="uid-fixture")
        event["parameters"] = {
            "node_name": "node-a",
            "restart_count": 3,
            "last_exit_reason": "OOMKilled",
        }
        self.open_card()
        expect(self.card.locator(".card-change")).to_contain_text("frontend-fixture")
        expect(self.card.locator(".card-change")).to_contain_text("uid-fixture")
        expect(self.card.locator(".card-change")).to_contain_text("node-a")
        expect(self.card.locator(".card-change")).to_contain_text("OOMKilled")
        expect(self.card.locator(".change-table")).to_have_count(0)
        self.assert_no_errors()

    def test_create_session_from_empty_list(self):
        self.fixture.sessions.clear()
        self.fixture.approval_enabled = False
        self.page.goto(self.fixture.url)
        create = self.page.get_by_role("button", name="新会话")
        expect(self.page.get_by_text("暂无会话")).to_be_visible()
        expect(self.page.locator("#sessions-access")).to_have_text("只读 · 浏览器缺少批准凭证")
        expect(create).to_be_disabled()
        self.assertEqual(self.fixture.posts, [])

        self.fixture.approval_enabled = True
        self.page.reload()
        expect(create).to_be_enabled()
        self.assertEqual(self.page.request.post(self.fixture.url + "/api/sessions").status, 403)
        create.click()
        expect(self.page).to_have_url(self.fixture.url + "/sessions/created-demo")
        expect(self.page.locator("#session-title")).to_have_text("新会话")
        expect(self.page.locator("#session-meta")).to_have_attribute("data-state", "idle")
        self.page.locator("#question").fill("查一下当前运行状态")
        self.page.get_by_role("button", name="发送").click()
        expect(self.page.locator(".message")).to_have_text("你查一下当前运行状态")
        self.assertEqual([action for action, _ in self.fixture.posts], ["create", "messages"])
        self.assert_no_errors()


if __name__ == "__main__":
    unittest.main(verbosity=2)
