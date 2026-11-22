import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { CardEvent, Session, SessionEvent } from "./api";
import { SessionRoute, Workspace } from "./workspace";

const access = { approval_enabled: true, approval_disabled_reason: null };
const ts = "2026-11-20T02:14:00Z";
const alpha = { id: "alpha", title: "甲会话", source: "user", state: "idle", last_seq: 0, pending_approval: false } satisfies Session;
const beta = { ...alpha, id: "beta", title: "乙会话" };
const sessions = [alpha, beta];
const json = (value: unknown) => Response.json(value);

function setup(
  handler: (url: string, init: RequestInit) => Response | Promise<Response>,
  list: () => Response | Promise<Response> = () => json({ sessions, ...access }),
) {
  vi.stubGlobal("fetch", (url: string, init: RequestInit) => {
    if (url === "/api/sessions") return Promise.resolve(list());
    if (url === "/api/monitoring") return Promise.resolve(json(Object.fromEntries(
      ["alerts", "probes", "changes", "services", "warnings", "nodes"].map((key) => [key, { status: "missing", data: null }]),
    )));
    return Promise.resolve(handler(url, init));
  });
  return render(<MemoryRouter initialEntries={["/sessions/alpha"]}>
    <Routes><Route element={<Workspace />}>
      <Route index element={<SessionRoute />} />
      <Route path="sessions/:sessionId" element={<SessionRoute />} />
    </Route></Routes>
  </MemoryRouter>);
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class {
    observe() {}
    disconnect() {}
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("切换会话时取消在途读取，忽略旧会话迟到的事件页", async () => {
  const late = Promise.withResolvers<Response>();
  let oldSignal: AbortSignal | null | undefined;
  setup((url, init) => {
    if (url === "/api/sessions/alpha") return json({ session: { ...alpha, last_seq: 1 }, ...access });
    if (url === "/api/sessions/beta") return json({ session: { ...beta, last_seq: 1 }, ...access });
    if (url.includes("/alpha/events?")) {
      oldSignal = init.signal;
      return late.promise;
    }
    return json({ next_after_seq: 1, has_more: false, events: [
      { type: "message", session_id: "beta", seq: 1, ts, role: "assistant", text: "乙的结果" },
    ] });
  });

  await waitFor(() => expect(oldSignal).toBeDefined());
  fireEvent.click(screen.getByRole("link", { name: /乙会话/ }));
  await screen.findByText("乙的结果", { selector: ".message p" });
  expect(oldSignal?.aborted).toBe(true);

  await act(async () => late.resolve(json({ next_after_seq: 1, has_more: false, events: [
    { type: "message", session_id: "alpha", seq: 1, ts, role: "assistant", text: "甲的迟到结果" },
  ] })));
  expect(screen.queryByText("甲的迟到结果")).toBeNull();
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("乙会话");
});

test("服务已入账但响应丢失，返回同会话重试草稿沿用原输入 ID", async () => {
  const ids: string[] = [];
  const persisted = new Map<string, SessionEvent>();
  const text = "当前几个副本？";
  setup((url, init) => {
    if (url === "/api/sessions/alpha") return json({ session: { ...alpha, last_seq: persisted.size }, ...access });
    if (url === "/api/sessions/beta") return json({ session: beta, ...access });
    if (url.includes("/events?")) {
      const cursor = Number(new URL(url, "http://localhost").searchParams.get("after_seq"));
      return json({
        next_after_seq: url.includes("/beta/") ? 0 : persisted.size, has_more: false,
        events: url.includes("/beta/") ? [] : [...persisted.values()].filter((event) => event.seq > cursor),
      });
    }
    expect(url).toBe("/api/sessions/alpha/messages");
    const input: { input_id: string; text: string } = JSON.parse(String(init.body));
    ids.push(input.input_id);
    if (!persisted.has(input.input_id)) persisted.set(input.input_id, {
      ...input, type: "message", role: "user", session_id: "alpha", ts, seq: persisted.size + 1,
    });
    if (ids.length === 1) return Promise.reject(new Error("响应丢失"));
    return json({ input_id: input.input_id });
  });

  async function send() {
    fireEvent.change(screen.getByRole("textbox", { name: "输入调查问题" }), { target: { value: text } });
    const send = screen.getByRole<HTMLButtonElement>("button", { name: "发送" });
    await waitFor(() => expect(send.disabled).toBe(false));
    fireEvent.click(send);
  }

  await send();
  await screen.findByText("响应丢失");
  fireEvent.click(screen.getByRole("link", { name: /乙会话/ }));
  await screen.findByRole("heading", { name: "乙会话" });
  fireEvent.click(screen.getByRole("link", { name: /甲会话/ }));
  await screen.findByText(text, { selector: ".message p" });
  await send();
  await waitFor(() => expect(ids).toHaveLength(2));
  expect(ids[0]).toBe(ids[1]);
  expect(persisted.size).toBe(1);
  expect(screen.getAllByText(text, { selector: ".message p" })).toHaveLength(1);
});

test("审批提交用完整卡片 ID 与指纹，回执入账前保持待审批并防止重复提交", async () => {
  const card: CardEvent = {
    type: "approval_required", seq: 1, session_id: "alpha", ts,
    card_id: "full-card-id", fingerprint: "f".repeat(64), status: "pending_approval", action: "restart_pod",
  };
  const response = Promise.withResolvers<Response>();
  const submissions: unknown[] = [];
  const events: SessionEvent[] = [card];
  setup((url, init) => {
    if (url === "/api/sessions/alpha") return json({
      session: { ...alpha, state: "awaiting_approval", last_seq: events.length }, ...access,
    });
    if (url === "/api/sessions/beta") return json({ session: beta, ...access });
    if (url.includes("/beta/events?")) return json({ next_after_seq: 0, has_more: false, events: [] });
    if (url.includes("/events?")) {
      const cursor = Number(new URL(url, "http://localhost").searchParams.get("after_seq"));
      return json({ next_after_seq: events.length, has_more: false, events: events.slice(cursor) });
    }
    expect(url).toBe("/api/sessions/alpha/cards/full-card-id/decision");
    submissions.push(JSON.parse(String(init.body)));
    return response.promise;
  });

  const approve = await screen.findByRole("button", { name: "批准" });
  const reject = screen.getByRole("button", { name: "拒绝" });
  fireEvent.click(approve);
  fireEvent.click(reject);
  expect(submissions).toEqual([{ fingerprint: card.fingerprint, decision: "approved" }]);
  await act(async () => response.resolve(json({ status: "approved" })));
  await screen.findByText("已提交 · 等待账本更新");
  expect(document.querySelector(".card-state")?.textContent).toBe("待审批");
  fireEvent.click(approve);
  fireEvent.click(screen.getByRole("link", { name: /乙会话/ }));
  await screen.findByRole("heading", { name: "乙会话" });
  fireEvent.click(screen.getByRole("link", { name: /甲会话/ }));
  fireEvent.click(await screen.findByRole("button", { name: "拒绝" }));
  await screen.findByText("已提交 · 等待账本更新");
  expect(submissions).toHaveLength(1);

  events.push({ type: "card_decision", seq: 2, session_id: "alpha", ts, card_id: card.card_id,
    status: "approved", expires_at: "2026-11-21T02:14:00Z" });
  await waitFor(() => expect(document.querySelector(".card-state")?.textContent).toBe("已批准 · 待执行"),
    { timeout: 2500 });
  expect(document.querySelector("#session-meta")?.getAttribute("data-state")).toBe("awaiting_approval");
});

test("改名保留编辑中的草稿，回执同步详情和会话列表并抵抗晚到读取", async () => {
  const save = Promise.withResolvers<Response>();
  const lateList = Promise.withResolvers<Response>();
  const requestSave = vi.fn(() => save.promise);
  let renders = 0;
  let detail = { ...alpha, title: "甲会话", last_seq: 1 };
  setup((url, init) => {
    if (url === "/api/sessions/alpha") return json({ session: detail, ...access });
    if (url.includes("/events?")) {
      return json({ next_after_seq: 1, has_more: false, events: [] });
    }
    expect(url).toBe("/api/sessions/alpha/title");
    expect(JSON.parse(String(init.body))).toEqual({ title: "人工草稿" });
    return requestSave();
  }, () => {
    renders += 1;
    return renders === 1 ? json({ sessions, ...access }) : lateList.promise;
  });

  const heading = await screen.findByRole("heading", { name: "甲会话" });
  fireEvent.click(within(heading).getByRole("button", { name: "甲会话" }));
  const title = screen.getByRole<HTMLInputElement>("textbox", { name: "会话名称" });
  fireEvent.change(title, { target: { value: "人工草稿" } });

  detail = { ...alpha, title: "轮询新名称", last_seq: 2 };
  await screen.findByRole("link", { name: /轮询新名称/ }, { timeout: 2000 });
  expect(title.value).toBe("人工草稿");
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(requestSave).toHaveBeenCalledTimes(1);
  await act(async () => save.resolve(json({ session: { ...detail, title: "人工名称", last_seq: 3 } })));
  await screen.findByRole("heading", { name: "人工名称" });
  await screen.findByRole("link", { name: /人工名称/ });
  await waitFor(() => expect(renders).toBeGreaterThan(1));
  await act(async () => lateList.resolve(json({ sessions, ...access })));
  expect(screen.getByRole("heading", { name: "人工名称" })).toBeTruthy();
  expect(screen.getByRole("link", { name: /人工名称/ })).toBeTruthy();
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "人工名称" }));
});
