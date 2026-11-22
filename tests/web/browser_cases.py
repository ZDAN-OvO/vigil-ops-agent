"""通过独立同源服务在 Chrome 检查网页，不使用数据库或集群。"""

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

    def open_card(self, anchor=CARD):
        self.page.goto(self.fixture.url + PATH + f"#card-{anchor}")
        expect(self.page.locator("#session-meta")).to_have_attribute(
            "data-state", "awaiting_approval"
        )
        if anchor == CARD:
            expect(self.card).to_have_class("card anchor-target")
            expect(self.card).to_be_focused()

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
                page.locator(".process-fold > summary").first.click()
                for control in page.locator(".observation button").all():
                    control.click()
                expect(page.locator(".observation .fold-body.open")).to_have_count(6)
                self.assert_no_overflow()
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
          document.querySelector('.conversation-scroll')
        ].filter(node => node.scrollWidth > node.clientWidth).map(node => node.className)"""),
            [],
        )

    def test_tool_card_survives_paging_reconnect_and_evidence_jump(self):
        self.fixture.duplicate_events = True
        original = self.fixture.events["alert-demo"][2]
        original.update(
            id="pod-call",
            status="running",
            summary="正在查询",
            collected_at=None,
            observation=None,
            observation_id=None,
        )
        self.open_card()
        page = self.page
        expect(page.locator("#ledger, .event-popover")).to_have_count(0)
        expect(page.get_by_text("主线 · 已送入 · 第 6 步")).to_be_visible()
        expect(page.get_by_text("旁路 · 已送入 · 第 2 步")).to_be_visible()
        expect(page.locator(".message[data-lane=side]").first).to_have_attribute(
            "data-turn", "side-demo"
        )
        expect(self.card.get_by_text("观测不可回查")).to_have_count(2)
        expect(page.locator(".hypothesis-basis").get_by_text("观测不可回查")).to_be_visible()
        cards = page.locator(".observation")
        expect(cards).to_have_count(6)
        first = cards.first
        expect(first).to_have_attribute("data-status", "running")
        node = first.element_handle()
        self.fixture.query_cursors.clear()
        before = len(self.fixture.events["alert-demo"])

        self.fixture.reject_reads = True
        expect(page.locator("#connection")).to_have_text("连接中断 · 正在重试")
        self.fixture.push("message", role="assistant", text="断开期间新增的消息")
        for number in range(105):
            self.fixture.push("evidence", evidence_id=f"page-{number}", observation_id="obs-0")
        self.fixture.push(
            "tool_call",
            id="pod-call",
            tool="k8s_pod_status",
            status="ok",
            summary="已取得观测",
            observation_id="obs-0",
            observation={"rows": 2, "token": SECRET, "path": LOCAL_PATH},
        )
        self.fixture.reject_reads = False

        expect(page.get_by_text("断开期间新增的消息")).to_have_count(1)
        expect(page.locator("#connection")).to_be_empty()
        expect(first).to_have_attribute("data-status", "ok")
        expect(cards).to_have_count(6)
        self.assertTrue(node.evaluate("(node) => node === document.querySelector('.observation')"))
        self.assertIn(before, self.fixture.query_cursors)
        self.assertIn(before + 100, self.fixture.query_cursors)
        self.assertNotIn(0, self.fixture.query_cursors)

        process = page.locator(".process-fold").first
        expect(first).to_be_hidden()
        reference = self.card.get_by_role("button", name="E1", exact=True)
        reference.click()
        expect(process).to_have_js_property("open", True)
        expect(first.locator(".fold-body.open")).to_be_visible()
        expect(first.get_by_role("button", name="展开或收起观测")).to_be_focused()
        first.get_by_role("button", name="返回卡片引用").click()
        expect(reference).to_be_focused()
        expect(first.locator(".observation-reference")).to_have_count(0)
        expect(first.locator("pre")).to_contain_text('"rows": 2')
        expect(first.locator("pre")).to_contain_text("[已遮盖]")
        expect(page.locator("body")).not_to_contain_text(SECRET)
        expect(page.locator("body")).not_to_contain_text(LOCAL_PATH)

        page.reload()
        expect(cards).to_have_count(6)
        expect(first).to_have_attribute("data-status", "ok")
        response = page.request.get(
            self.fixture.url
            + PATH.replace("/sessions", "/api/sessions")
            + f"/events?after_seq={before + 100}&limit=100"
        )
        self.assertEqual(response.json()["events"][-1]["seq"], before + 107)
        self.assertNotIn(SECRET, response.text())
        self.assertNotIn(LOCAL_PATH, response.text())
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
        heading = self.page.locator("h1#session-title")
        expect(heading).to_have_text("首页探测失败")
        expect(heading.get_by_role("button")).to_have_count(0)
        heading.click()
        expect(self.page.get_by_role("textbox", name="会话名称")).to_have_count(0)
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
        expect(page.locator(".message").first.locator(".stamp").first).to_have_text("告警")
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
        expect(self.page.locator(".message")).to_contain_text("查一下当前运行状态")
        expect(self.page.get_by_text("已排队 · 等待送达")).to_be_visible()
        expect(self.page.locator("#session-title")).to_have_text("查一下当前运行状态")
        self.assertEqual([action for action, _ in self.fixture.posts], ["create", "messages"])
        self.assert_no_errors()

    def test_process_groups_keep_messages_and_approval_in_order_on_replay(self):
        fixture = self.fixture
        fixture.push(
            "tool_call",
            tool="kubernetes.list_pods",
            id="pending-query",
            lane="main",
            status="running",
        )
        fixture.push(
            "skill", name="check-pods", trigger="description", status="failed", lane="main"
        )
        fixture.push(
            "tool_call",
            tool="kubernetes.list_pods",
            lane="main",
            status="empty",
            summary="此查询范围内没有匹配 Pod",
        )
        fixture.push("message", role="user", text="需要先对比另一个服务", input_id="extra-question")
        fixture.push(
            "tool_call",
            tool="kubernetes.list_pods",
            lane="main",
            status="rejected",
            summary="查询预算不足",
        )
        fixture.push("message", role="assistant", text="已记录补充线索", lane="main")
        fixture.push(
            "tool_call",
            tool="kubernetes.list_events",
            lane="side",
            turn_id="side-demo",
            status="error",
            summary="时限内未返回事件",
        )
        fixture.push("result", status="completed")
        self.open_card()
        page = self.page
        process = page.locator(".investigation-process")
        expect(process).to_have_count(5)
        expect(process.first.locator("summary")).to_have_text("调查过程 6 次查询")
        expect(self.card.get_by_role("button", name="批准")).to_be_visible()
        expect(process.nth(2).locator(".process-notices li")).to_contain_text(
            [
                "查询未完成",
                "Skill 读取失败",
                "此查询范围内没有匹配 Pod",
            ]
        )
        expect(
            process.nth(3).locator(".process-notices").get_by_text("查询预算不足", exact=False)
        ).to_be_visible()
        expect(
            process.nth(4).locator(".process-notices").get_by_text("时限内未返回事件", exact=False)
        ).to_be_visible()
        expect(process.nth(2).locator(".observation").first).to_be_hidden()

        survey = page.locator("#event-stream .feed-entry")
        position = survey.count() - 6
        items = survey.all_text_contents()[position:]
        for text, item in zip(
            (
                "待审批",
                "2 次查询",
                "需要先对比另一个服务",
                "查询预算不足",
                "已记录补充线索",
                "旁路",
            ),
            items,
            strict=True,
        ):
            self.assertIn(text, item)

        page.reload()
        expect(process).to_have_count(5)
        self.assertEqual(survey.all_text_contents()[position:], items)
        expect(self.card.get_by_role("button", name="批准")).to_be_visible()
        self.assert_no_errors()

    def test_hypothesis_history_observation_and_skill(self):
        self.open_card()
        page = self.page
        hypothesis = page.locator(".hypothesis")
        expect(hypothesis).to_have_count(1)
        node = hypothesis.element_handle()
        basis = hypothesis.locator(".hypothesis-basis")
        expect(basis).to_contain_text("新版本发布后入口未就绪")
        expect(hypothesis.locator(".hypothesis-state")).to_have_css("font-size", "13px")
        expect(hypothesis.locator(".hypothesis-state > span")).to_have_css(
            "color", "rgb(236, 232, 225)"
        )
        expect(page.locator("#session-meta .warn")).to_have_text("待审批")
        expect(page.locator("#session-meta .warn")).to_have_css("color", COLORS["warn"])
        reference = basis.get_by_role("button", name="查看观测")
        observation = page.locator(".observation").first
        original_observation = observation.element_handle()
        disclosure = observation.get_by_role("button", name="展开或收起观测")
        page.locator(".process-fold > summary").first.click()
        disclosure.click()
        expect(observation.locator(".observation-reference")).to_have_count(0)
        page.locator(".process-fold > summary").first.click()

        reference.click()
        expect(observation.locator(".fold-body.open")).to_be_visible()
        context = observation.locator(".observation-reference")
        expect(context).to_be_in_viewport()
        expect(context).to_contain_text("坏发布导致首页探测失败")
        expect(context).to_contain_text("支持 新版本发布后入口未就绪")
        expect(observation.get_by_text("demo / public-fixture")).to_be_visible()
        expect(observation.get_by_text("未公开")).to_be_visible()
        expect(observation.get_by_text("时间范围")).to_be_visible()
        expect(disclosure).to_be_focused()
        context.get_by_role("button", name="返回依据").click()
        expect(reference).to_be_focused()
        expect(reference).to_be_in_viewport()
        expect(context).to_have_count(0)
        self.assertTrue(
            original_observation.evaluate(
                "(node) => node === document.querySelector('.observation')"
            )
        )

        skill = page.locator(".skill-details")
        expect(skill).to_be_hidden()
        page.locator(".process-fold > summary").filter(has_text="1 项 Skill").click()
        expect(skill.locator("pre")).not_to_be_visible()
        skill.locator("summary").click()
        expect(skill.locator("pre")).to_have_text("核对副本和发布版本")
        self.fixture.push(
            "evidence",
            evidence_id="e-1",
            observation_id="obs-0",
            hypothesis_id="h1",
            relation="refuting",
            rationale="扩大时间范围后证据转向",
        )
        self.fixture.push(
            "hypothesis",
            id="h1",
            statement="坏发布导致首页探测失败",
            status="refuted",
            update_reason="排除了发布时序巧合",
        )
        expect(hypothesis).to_have_attribute("data-status", "refuted")
        self.assertTrue(node.evaluate("(node) => node === document.querySelector('.hypothesis')"))
        expect(basis).to_contain_text("扩大时间范围后证据转向")
        expect(basis).not_to_contain_text("新版本发布后入口未就绪")
        hypothesis.locator("summary").click()
        history = hypothesis.locator(".hypothesis-history li")
        expect(history).to_have_count(3)
        expect(history.nth(1)).to_contain_text("新版本发布后入口未就绪")
        expect(history.nth(1)).not_to_contain_text("扩大时间范围后证据转向")
        expect(history.last).to_contain_text("排除了发布时序巧合")
        messages = page.locator(".message[data-message-id]").all_text_contents()
        self.assertLess(
            next(i for i, text in enumerate(messages) if "旁路回复" in text),
            next(i for i, text in enumerate(messages) if "主线回复" in text),
        )
        self.assert_no_errors()

    def test_resize_persist_reset_and_open_narrow_panels(self):
        self.open_card()
        page = self.page
        left = page.get_by_role("separator", name="调整会话栏宽度")
        right = page.get_by_role("separator", name="调整值班面板宽度")
        left.focus()
        left.press("ArrowRight")
        expect(left).to_have_attribute("aria-valuenow", "256")
        left.press("Home")
        expect(left).to_have_attribute("aria-valuenow", "180")
        left.press("End")
        expect(left).to_have_attribute("aria-valuenow", "360")
        bounds = right.bounding_box()
        x, y = bounds["x"] + bounds["width"] / 2, bounds["y"] + 40
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x - 40, y, steps=4)
        page.mouse.up()
        expect(right).to_have_attribute("aria-valuenow", "400")
        page.get_by_role("button", name="收起值班面板").click()
        page.reload()
        expect(left).to_have_attribute("aria-valuenow", "360")
        expect(page.locator(".monitoring")).to_have_css("width", "42px")
        page.get_by_role("button", name="展开值班面板").click()
        expect(right).to_have_attribute("aria-valuenow", "400")
        page.get_by_role("button", name="恢复默认分栏").click()
        expect(left).to_have_attribute("aria-valuenow", "240")
        expect(right).to_have_attribute("aria-valuenow", "360")
        page.set_viewport_size({"width": 1050, "height": 800})
        expect(page.locator(".conversation")).to_have_css("width", "480px")
        self.assert_no_overflow()
        page.set_viewport_size({"width": 850, "height": 800})
        expect(page.locator(".monitoring")).to_have_css("width", "42px")
        page.get_by_role("button", name="展开值班面板").click()
        expect(page.locator("#monitor-panel")).to_be_visible()
        expect(page.locator("#monitoring-content")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.locator("#monitoring-content")).to_be_hidden()

        page.set_viewport_size({"width": 390, "height": 800})
        expect(page.get_by_role("separator")).to_have_count(0)
        expect(page.locator("#sessions-panel")).to_be_hidden()
        expect(page.locator("#monitor-panel")).to_be_hidden()
        self.assert_no_overflow()
        sessions = page.get_by_role("button", name="会话", exact=True)
        sessions.click()
        expect(page.get_by_role("button", name="关闭会话栏")).to_be_focused()
        self.assert_no_overflow()
        page.keyboard.press("Escape")
        expect(sessions).to_be_focused()
        monitor = page.get_by_role("button", name="值班面板", exact=True)
        monitor.click()
        expect(page.get_by_role("button", name="收起值班面板")).to_be_focused()
        self.assert_no_overflow()
        page.get_by_role("button", name="收起值班面板").click()
        expect(monitor).to_be_focused()
        self.assert_no_errors()

    def test_rename_session_stays_after_reload(self):
        self.open_card()
        page = self.page
        title = page.locator("h1#session-title").get_by_role("button", name="首页探测失败")
        title.focus()
        title.press("Enter")
        name = page.get_by_role("textbox", name="会话名称")
        expect(name).to_be_focused()
        name.fill("临时草稿")
        name.press("Escape")
        expect(title).to_be_focused()
        expect(page.locator("#session-title")).to_have_text("首页探测失败")

        page.get_by_role("button", name="改名").click()
        name.fill("字" * 121)
        page.get_by_role("button", name="保存").click()
        expect(page.locator(".title-form .notice")).to_have_text("请求字段或格式错误")
        expect(name).to_have_value("字" * 121)
        name.fill(" 值班   排查 ")
        page.get_by_role("button", name="保存").click()
        expect(page.locator("#session-title")).to_have_text("值班 排查")
        expect(page.locator(".session-row.selected .session-name")).to_have_text("值班 排查")
        page.reload()
        expect(page.locator("#session-title")).to_have_text("值班 排查")
        expect(self.card).to_be_visible()
        self.assert_no_errors()


if __name__ == "__main__":
    unittest.main(verbosity=2)
