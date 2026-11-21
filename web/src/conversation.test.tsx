import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ObservationEvent, SessionEvent } from "./api";
import { Conversation } from "./conversation";

const ts = "2026-11-20T02:14:00Z";
const envelope = { session_id: "alpha", turn_id: "main", ts };
const options = {
  access: { approval_enabled: false, approval_disabled_reason: null },
  decide: vi.fn(), anchor: "", notice: "",
};

function query(seq: number, fields: Partial<ObservationEvent> = {}): ObservationEvent {
  return { ...envelope, type: "tool_call", seq, id: "reused", status: "running", summary: "正在查询",
    tool: "kubernetes.list_pods", ...fields };
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

test("同调用原位更新并保留展开状态，同名不同 ID 和不同轮次各自显示", () => {
  const first = query(1);
  const other = query(2, { id: "other" });
  const side = query(3, { turn_id: "side" });
  const later = query(4, { turn_id: "next" });
  const historical = query(5, { id: undefined });
  const partial = [first, other, side, later, historical];
  const container = render(<Conversation {...options} events={partial} />);
  const cards = () => Array.from(container.container.querySelectorAll(".observation"));
  const original = cards()[0] as HTMLElement;
  const trigger = within(original).getByRole("button");
  fireEvent.click(trigger);
  const complete = query(6, {
    status: "ok", summary: "已取得观测", observation: "两条 Pod 观测", collected_at: "2026-11-20T02:15:38Z",
  });
  const full = [...partial, complete, query(7), query(8, { id: undefined, status: "error", summary: "查询失败" })];
  container.rerender(<Conversation {...options} events={full} />);
  expect(cards()).toHaveLength(6);
  expect(cards()[0]).toBe(original);
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(original.textContent).toContain(":15:38");
  expect(original.textContent).toContain("两条 Pod 观测");
  expect(original.textContent).not.toContain("正在查询");
  expect(cards().map((card) => card.getAttribute("data-status")))
    .toEqual(["ok", "running", "running", "running", "running", "error"]);
  container.unmount();
  const replay = render(<Conversation {...options} events={full} />);
  expect(replay.container.querySelectorAll(".observation")).toHaveLength(6);
  expect(replay.container.querySelector(".observation")?.getAttribute("data-status")).toBe("ok");
});

test("轮次终结仅移除本轮遗留的查询中状态，并保留失败提示", () => {
  const events: SessionEvent[] = [
    query(1), query(2, { turn_id: "side" }),
    { ...envelope, type: "result", status: "failed", summary: "模型调用失败", seq: 3 },
  ];
  render(<Conversation {...options} events={events} />);
  expect(screen.getByText("查询未完成")).toBeTruthy();
  expect(screen.getByText("模型调用失败")).toBeTruthy();
  expect(screen.getAllByText("正在查询", { selector: ".observation-summary" })).toHaveLength(1);
});

test("主线、旁路、委派和历史观测的来源在结果回放后仍可区分", () => {
  const events = [
    query(1, { id: "main", lane: "main", delegated: false }),
    query(2, { id: "side", turn_id: "side", lane: "side", delegated: false }),
    query(3, { id: "delegated", lane: "main", delegated: true }),
    query(4, { id: "historical" }),
  ];
  const { container, rerender } = render(<Conversation {...options} events={events} />);
  const labels = () => Array.from(container.querySelectorAll(".observation-head"))
    .map((node) => node.textContent);
  expect(labels()[0]).toContain("主线");
  expect(labels()[1]).toContain("旁路");
  expect(labels()[2]).toContain("主线 · 只读委派");
  expect(labels()[3]).toContain("来源未记录");
  rerender(<Conversation {...options} events={[
    ...events,
    ...events.map((event, index) => ({ ...event, seq: index + 5, status: "ok" as const })),
  ]} />);
  expect(container.querySelectorAll(".observation")).toHaveLength(4);
  expect(labels()[1]).toContain("旁路");
  expect(labels()[2]).toContain("只读委派");
  expect(labels()[3]).toContain("来源未记录");
});

test("同一轮的最终回复与临时说明相同只显示一次", () => {
  const interim = { ...envelope, type: "message", role: "assistant", text: "已核对副本", interim: true, seq: 1 } as const;
  const { rerender } = render(<Conversation {...options} events={[interim, query(2)]} />);
  expect(screen.getAllByText(interim.text)).toHaveLength(1);
  rerender(<Conversation {...options} events={[interim, query(2), { ...interim, interim: false, seq: 3 }]} />);
  expect(screen.getAllByText(interim.text)).toHaveLength(1);
});

test("助手 Markdown 安全渲染，普通用户文本及工具明细保留字面内容", () => {
  const text = [
    "## 核查结果", "", "- **就绪**与 *延迟*", "", "> 观测记录", "",
    "| 服务 | 状态 |", "| --- | --- |", "| checkout | Ready |", "",
    "使用 `get`。", "", "```text", "output", "```", "",
    "[参考](https://example.org/docs) [危险](javascript:alert%281%29)",
    "[其他协议](data:text/html,hello) ![入口截图](https://example.org/image.png)",
    '<img src="https://example.org/raw.png" onerror="xss()">',
  ].join("\n");
  const events: SessionEvent[] = [
    { ...envelope, type: "message", role: "assistant", seq: 1, text },
    { ...envelope, type: "message", role: "user", seq: 2, text: "## 用户输入" },
    query(3, { status: "ok", observation: "**工具原文**" }),
  ];
  const { container } = render(<Conversation {...options} events={events} />);
  expect(screen.getByRole("heading", { name: "核查结果" })).toBeTruthy();
  expect(screen.getByRole("listitem").querySelector("strong")?.textContent).toBe("就绪");
  expect(container.querySelector("em")?.textContent).toBe("延迟");
  expect(container.querySelector("blockquote")?.textContent?.trim()).toBe("观测记录");
  expect(within(screen.getByRole("table")).getByRole("cell", { name: "Ready" })).toBeTruthy();
  expect(container.querySelector(".assistant-markdown pre code")?.textContent).toBe("output\n");
  expect(screen.getByText("get").tagName).toBe("CODE");
  const link = screen.getByRole("link", { name: "参考" });
  expect(link.getAttribute("href")).toBe("https://example.org/docs");
  expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(container.querySelector("img")).toBeNull();
  expect(screen.getByText(/入口截图/)).toBeTruthy();
  expect(screen.getByText("## 用户输入").tagName).toBe("P");
  expect(container.querySelector(".observation pre")?.textContent).toBe("**工具原文**");
});
