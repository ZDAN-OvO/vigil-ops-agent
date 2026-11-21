"""用生产 Web 入口、真实会话账本与受控模型验收浏览器里的线性对话。"""

import asyncio
import shutil
import socket
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from investigation_support import ScriptedChatModel, call, settings
from langchain_core.messages import AIMessage
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


@pytest.mark.skipif(shutil.which("google-chrome") is None, reason="未安装 Google Chrome")
def test_live_conversation_updates_tool_card_renders_markdown_and_projects_csp(
    isolated_serve_dsn, tmp_path
):
    async def run():
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
                deploys(),
                AIMessage(content=ANSWER),
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
                    await verify_browser(config.web_origin, auth, tool)
                finally:
                    tool.reply.set()
                    stop.set()
                    server.should_exit = True
                    await asyncio.gather(server_task, queue)

    asyncio.run(run())


async def verify_browser(origin, auth, tool):
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
        await asyncio.wait_for(tool.called.wait(), 12)
        await expect(page.locator(".assistant-markdown strong")).to_have_text("排查方法")
        await expect(page.get_by_text("pod-rebuild · 已加载 · 主动读取")).to_be_visible()

        card = page.locator(".observation")
        await expect(card).to_have_count(1)
        await expect(card).to_have_attribute("data-status", "running")
        node = await card.element_handle()
        disclosure = card.get_by_role("button", name="展开或收起观测")
        await disclosure.click()
        tool.reply.set()
        await expect(card).to_have_attribute("data-status", "ok")
        await expect(card).to_have_count(1)
        assert await node.evaluate("(node) => node === document.querySelector('.observation')")
        await expect(disclosure).to_have_attribute("aria-expanded", "true")
        await expect(card.locator("pre")).to_contain_text('"replicas": 2')
        await expect(card).not_to_contain_text("正在查询")

        answer = page.locator(".assistant-markdown").last
        await expect(answer.get_by_role("heading", name="副本核对结果")).to_be_visible()
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
        public = await response.text()
        assert all(marker not in public for marker in (SECRET, PATH, PRIVATE))
        await page.reload()
        await expect(card).to_have_count(1)
        await expect(card).to_have_attribute("data-status", "ok")
        assert errors == []
        assert external == []
        await browser.close()
