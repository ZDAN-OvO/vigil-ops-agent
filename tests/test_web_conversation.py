"""用生产 Web 入口、真实会话账本与受控模型验收浏览器里的线性对话。"""

import asyncio
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from investigation_support import ScriptedChatModel, call, report_call, settings
from langchain_core.messages import ToolMessage
from playwright.async_api import async_playwright, expect
from test_agent_conversation import BlockingToolbox, deploys

from aiops.actions.safety import ActionSafety
from aiops.agent.conversation import ConversationDriver
from aiops.db import Store
from aiops.serve import _Server, drive_queue
from aiops.sessions.checkpoint import setup_checkpoints
from aiops.web_api import PAGE_CSP, BrowserAuth, build_web_app

SECRET = "test-assistant-secret"
PATH = "/home/browser-fixture/credentials"
PRIVATE = "test-private-thinking"
ANSWER = "\n".join(
    [
        "## 副本核对结果",
        "",
        "- **副本就绪**，已核对 `checkout`。",
        "",
        "> 来源为本轮查询。",
        "",
        "| 服务 | 副本 |",
        "| :--- | ---: |",
        "| checkout | 2 |",
        "",
        "```text",
        "a" * 160,
        "```",
        "",
        "[参考](https://example.org/docs) [危险](javascript:alert%281%29)",
        "",
        "![入口截图](https://example.org/image.png)",
        "",
        '<img src="https://example.org/raw.png" onerror="alert(1)">',
        "",
        f"token={SECRET} {PATH}",
    ]
)


def chrome_available():
    if sys.platform != "win32":
        return shutil.which("google-chrome") is not None
    return any(
        (Path(root) / "Google/Chrome/Application/chrome.exe").is_file()
        for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")
        if (root := os.environ.get(name))
    )


@pytest.mark.skipif(not chrome_available(), reason="未安装 Google Chrome")
def test_live_conversation_updates_tool_card_renders_markdown_and_projects_csp(
    isolated_serve_dsn, tmp_path
):
    async def run():
        def assess(messages):
            outputs = {
                message.name: json.loads(message.content)
                for message in messages
                if isinstance(message, ToolMessage)
                and message.name in {"manage_hypotheses", "kubernetes_list_deployments"}
            }
            hypothesis_id = outputs["manage_hypotheses"]["hypotheses"][0]["id"]
            observation_id = outputs["kubernetes_list_deployments"]["observation_id"]
            return call(
                "assess_evidence",
                {
                    "links": [
                        {
                            "observation_id": observation_id,
                            "hypothesis_id": hypothesis_id,
                            "relation": "refuting",
                            "rationale": "观测到两个就绪副本",
                        }
                    ],
                    "hypothesis_updates": [
                        {
                            "hypothesis_id": hypothesis_id,
                            "status": "refuted",
                            "reason": "副本已核对",
                        }
                    ],
                },
                "assessment",
            )

        tool = BlockingToolbox()
        tool.responses["kubernetes.list_deployments"] = {"data": {"items": [{"replicas": 2}]}}
        read = call("read_file", {"file_path": "/pod-rebuild/SKILL.md"}, "read-skill")
        model = ScriptedChatModel(
            responses=[
                read.model_copy(
                    update={
                        "content": [
                            {"type": "thinking", "thinking": PRIVATE},
                            {"type": "text", "text": "先核对 **排查方法**。"},
                        ],
                        "additional_kwargs": {"reasoning_content": PRIVATE},
                    }
                ),
                call(
                    "manage_hypotheses",
                    {
                        "operations": [{"op": "create", "statement": "副本数量不足"}],
                    },
                    "hypothesis",
                ),
                deploys(),
                assess,
                report_call(confirmed=False, conclusion=ANSWER, boundary="仅覆盖本次副本查询"),
            ]
        )
        async with Store.open(isolated_serve_dsn) as store:
            await store.migrate()
            await setup_checkpoints(store)
            with socket.create_server(("127.0.0.1", 0)) as listener:
                config = settings(runtime_dir=tmp_path, web_port=listener.getsockname()[1])
                driver = ConversationDriver(settings=config, store=store, toolbox=tool, model=model)
                safety = ActionSafety(store)
                auth = BrowserAuth(interactive=True)
                app = build_web_app(config, sessions=driver.sessions, safety=safety, auth=auth)
                server = _Server(uvicorn.Config(app, access_log=False, ws="none"))
                server_task = asyncio.create_task(server.serve(sockets=[listener]))
                stop = asyncio.Event()
                queue = asyncio.create_task(drive_queue(driver, safety, stop))
                try:
                    async with asyncio.timeout(5):
                        while not server.started:
                            if server_task.done():
                                await server_task
                                raise RuntimeError("生产 Web 测试服务提前退出")
                            await asyncio.sleep(0.01)
                    loop = asyncio.get_running_loop()
                    query_started = asyncio.run_coroutine_threadsafe(tool.called.wait(), loop)

                    def release_query():
                        loop.call_soon_threadsafe(tool.reply.set)

                    def run_browser():
                        # Windows 的 psycopg 需要 Selector，Playwright 子进程需要 Proactor。
                        loop_factory = (
                            asyncio.ProactorEventLoop if sys.platform == "win32" else None
                        )
                        with asyncio.Runner(loop_factory=loop_factory) as runner:
                            runner.run(
                                verify_browser(
                                    config.web_origin, auth, query_started, release_query
                                )
                            )

                    try:
                        await asyncio.to_thread(run_browser)
                    finally:
                        query_started.cancel()
                finally:
                    tool.reply.set()
                    stop.set()
                    server.should_exit = True
                    await asyncio.gather(server_task, queue)

    asyncio.run(run())


async def verify_browser(origin, auth, query_started, release_query):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome")
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        token = parse_qs(urlsplit(auth.login_url(origin)).fragment)["token"][0]
        login = await context.request.post(
            f"{origin}/login", form={"token": token}, headers={"origin": origin}, max_redirects=0
        )
        assert login.status == 303
        page = await context.new_page()
        external = []
        errors = []
        page.on(
            "request",
            lambda request: (
                external.append(request.url) if not request.url.startswith(f"{origin}/") else None
            ),
        )
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda msg: (
                errors.append(msg.text)
                if msg.type == "error" and "Content Security Policy" in msg.text
                else None
            ),
        )
        document = await page.goto(origin)
        assert document.headers["content-security-policy"] == PAGE_CSP
        await page.get_by_role("button", name="新会话").click()
        await expect(page.locator("#session-meta")).to_have_attribute("data-state", "idle")
        await page.get_by_role("textbox", name="输入调查问题").fill("核对 checkout 副本")
        await page.get_by_role("button", name="发送").click()
        await asyncio.wait_for(asyncio.wrap_future(query_started), 12)
        await expect(page.locator(".assistant-markdown strong")).to_have_text("排查方法")
        skill = page.locator(".skill-details").filter(
            has=page.locator("summary").filter(has_text="pod-rebuild")
        )
        await expect(skill).to_have_count(1)
        skill_process = page.locator(".process-fold").filter(has=skill)
        await skill_process.locator(":scope > summary").click()
        await skill.locator("summary").click()
        await expect(skill.locator("pre")).to_contain_text("name: pod-rebuild")
        await expect(page.locator("#session-title")).to_have_text("核对 checkout 副本")

        card = page.locator(".observation")
        await expect(card).to_have_count(1)
        await expect(card).to_have_attribute("data-status", "running")
        node = await card.element_handle()
        query_process = page.locator(".process-fold").filter(has=card)
        await query_process.locator(":scope > summary").click()
        disclosure = card.get_by_role("button", name="展开或收起观测")
        await disclosure.click()
        release_query()
        await expect(card).to_have_attribute("data-status", "ok")
        await expect(card).to_have_count(1)
        assert await node.evaluate("(node) => node === document.querySelector('.observation')")
        await expect(disclosure).to_have_attribute("aria-expanded", "true")
        await expect(card.locator("pre")).to_contain_text('"replicas": 2')
        await expect(card).not_to_contain_text("正在查询")
        hypothesis = page.locator(".hypothesis")
        await expect(hypothesis).to_have_attribute("data-status", "refuted")
        await expect(hypothesis.locator(".hypothesis-basis")).to_contain_text("观测到两个就绪副本")
        await disclosure.click()
        await query_process.locator(":scope > summary").click()
        await expect(card).to_be_hidden()
        await hypothesis.get_by_role("button", name="查看观测").click()
        await expect(query_process).to_have_js_property("open", True)
        await expect(disclosure).to_be_focused()
        await expect(card.locator("pre")).to_be_visible()

        await page.locator("h1#session-title").get_by_role("button").click()
        await page.get_by_role("textbox", name="会话名称").fill("  副本   值班  ")
        await page.get_by_role("button", name="保存").click()
        await expect(page.locator("#session-title")).to_have_text("副本 值班")
        await expect(page.locator(".session-row.selected .session-name")).to_have_text("副本 值班")

        answer = page.locator(".assistant-markdown").last
        await expect(answer.get_by_role("heading", name="副本核对结果")).to_be_visible()
        await expect(page.locator("#session-meta")).to_have_attribute("data-state", "idle")
        await expect(answer.get_by_role("listitem")).to_contain_text("副本就绪")
        await expect(answer.get_by_role("cell", name="2", exact=True)).to_be_visible()
        await expect(answer.locator("pre code")).to_have_text("a" * 160)
        await expect(answer.locator("pre")).to_have_css("overflow-x", "auto")
        await expect(answer.locator(".markdown-table")).to_have_css("overflow-x", "auto")
        assert await page.locator(".conversation-scroll").evaluate(
            "(node) => node.scrollWidth <= node.clientWidth"
        )
        await expect(answer.get_by_role("link")).to_have_count(1)
        await expect(answer.get_by_role("link", name="参考")).to_have_attribute(
            "rel", "noopener noreferrer"
        )
        await expect(answer).to_contain_text("入口截图")
        await expect(answer.locator("img, [style]")).to_have_count(0)
        for marker in (SECRET, PATH, PRIVATE):
            await expect(page.locator("#event-stream")).not_to_contain_text(marker)
        await expect(page.locator("#ledger, .event-popover")).to_have_count(0)

        events_url = f"{origin}/api{urlsplit(page.url).path}/events?after_seq=0"
        response = await context.request.get(events_url)
        assert response.ok
        events = (await response.json())["events"]
        tool_events = [event for event in events if event["type"] == "tool_call"]
        assert [e["status"] for e in tool_events] == ["running", "ok"]
        assert tool_events[0]["id"] == tool_events[1]["id"]
        assert tool_events[0]["seq"] < tool_events[1]["seq"]
        assert tool_events[1]["query_scope"]["criteria_withheld"]
        assert tool_events[1]["summary"] == "返回 1 个 Deployment"
        assert [e["update_reason"] for e in events if e["type"] == "hypothesis"] == [
            "模型创建假设",
            "副本已核对",
        ]
        assert [e["status"] for e in events if e["type"] == "result"] == ["completed"]
        public = await response.text()
        assert all(marker not in public for marker in (SECRET, PATH, PRIVATE))
        await page.reload()
        await expect(page.locator("#session-title")).to_have_text("副本 值班")
        await expect(hypothesis).to_have_attribute("data-status", "refuted")
        await expect(card).to_have_count(1)
        await expect(card).to_have_attribute("data-status", "ok")
        assert errors == []
        assert external == []
        await browser.close()
