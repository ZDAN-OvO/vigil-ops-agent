import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import type { ObservationEvent, SessionEvent } from "./api";
import { Conversation } from "./conversation";
import { projectConversation } from "./conversation-projection";

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
  const trigger = original.querySelector<HTMLButtonElement>("button")!;
  expect(original.closest("details.process-fold")?.hasAttribute("open")).toBe(false);
  fireEvent.click(original.closest("details.process-fold")!.querySelector("summary")!);
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
  expect(screen.getByText("查询未完成", { selector: ".process-notices .stamp" })).toBeTruthy();
  expect(screen.getByText("模型调用失败")).toBeTruthy();
  expect(screen.getAllByText("正在查询", { selector: ".observation-head .stamp" })).toHaveLength(1);
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

test("假设原位更新，历史依据按 seq 取当时最新关系，原始观测只在本轮查找", () => {
  const initial: SessionEvent[] = [
    { ...envelope, type: "hypothesis", seq: 1, id: "h", statement: "部署影响入口", status: "pending" },
    query(2, { status: "ok", observation_id: "obs", observation: "主线原始明细", query_scope: {
      cluster: "lab", namespace: "shop", criteria_withheld: true,
    } }),
    { ...envelope, type: "evidence", seq: 3, hypothesis_id: "h", evidence_id: "e",
      observation_id: "obs", relation: "supporting", rationale: "首批采样支持" },
    { ...envelope, type: "hypothesis", seq: 4, id: "h", statement: "部署影响入口", status: "supported",
      update_reason: "初步支持" },
  ];
  const { container, rerender } = render(<Conversation {...options} events={initial} />);
  const original = container.querySelector<HTMLElement>(".hypothesis")!;
  expect(within(original).getByText("有依据支持", { selector: ".hypothesis-state > span" })).toBeTruthy();
  const initialBasis = within(original.querySelector<HTMLElement>(".hypothesis-basis")!);
  expect(initialBasis.getByText("首批采样支持")).toBeTruthy();
  fireEvent.click(initialBasis.getByRole("button", { name: "查看观测" }));
  const trigger = screen.getByRole("button", { name: "展开或收起观测" });
  expect(trigger.getAttribute("aria-expanded")).toBe("true");
  expect(trigger.closest("details.process-fold")?.hasAttribute("open")).toBe(true);
  expect(document.activeElement).toBe(trigger);
  expect(screen.getByText("lab / shop")).toBeTruthy();
  expect(screen.getByText("未公开")).toBeTruthy();

  const rest: SessionEvent[] = [
    { ...envelope, type: "evidence", seq: 5, hypothesis_id: "h", evidence_id: "e",
      observation_id: "obs", relation: "refuting", rationale: "扩大窗口后不成立" },
    { ...envelope, type: "hypothesis", seq: 6, id: "h", statement: "部署影响入口", status: "refuted",
      update_reason: "窗口已复核" },
    { ...envelope, turn_id: "other", type: "hypothesis", seq: 7, id: "h", statement: "后续调查", status: "inconclusive" },
    { ...envelope, turn_id: "other", type: "evidence", seq: 8, hypothesis_id: "h", evidence_id: "e",
      observation_id: "obs", relation: "neutral", rationale: "旧观测不可作为本轮引用" },
  ];
  const all = [...initial, ...rest, initial[2]];
  rerender(<Conversation {...options} events={all} />);
  const hypotheses = container.querySelectorAll<HTMLElement>(".hypothesis");
  expect(hypotheses).toHaveLength(2);
  expect(hypotheses[0]).toBe(original);
  const basis = within(original.querySelector<HTMLElement>(".hypothesis-basis")!);
  expect(basis.getByText("扩大窗口后不成立")).toBeTruthy();
  expect(basis.queryByText("首批采样支持")).toBeNull();
  expect(within(hypotheses[1]).getByText("观测不可回查")).toBeTruthy();
  const history = original.querySelector(".hypothesis-history")!;
  fireEvent.click(history.querySelector("summary")!);
  const versions = Array.from(history.querySelectorAll("li"), (entry) => entry.textContent);
  expect(versions).toEqual([
    expect.stringContaining("待核实"),
    expect.stringContaining("初步支持"),
    expect.stringContaining("窗口已复核"),
  ]);
  expect(versions[0]).not.toContain("首批采样支持");
  expect(versions[1]).toContain("首批采样支持");
  expect(versions[1]).not.toContain("扩大窗口后不成立");
  expect(versions[2]).toContain("扩大窗口后不成立");
  expect(projectConversation(all).rows).toEqual(projectConversation([...initial, ...rest]).rows);
});

test("按账本序显示排队、主线、旁路、告警和助手正文，使用运行时正文部件", () => {
  const events = [
    { ...envelope, type: "message", role: "user", text: "尚未领取", input_id: "wait", seq: 1, turn_id: null },
    { ...envelope, type: "message", role: "user", text: "暂等审批时发出", input_id: "main", seq: 2, turn_id: null },
    { ...envelope, type: "message", role: "user", text: "旁路问", input_id: "side", seq: 3, turn_id: null },
    { ...envelope, type: "input_delivered", input_id: "side", seq: 4, turn_id: "other", step: 2, lane: "side" },
    { ...envelope, type: "message", role: "assistant", text: "旁路答", lane: "side", seq: 5, turn_id: "other" },
    { ...envelope, type: "message", role: "user", text: "告警再次触发", source: "alert", seq: 6 },
    { ...envelope, type: "input_delivered", input_id: "main", seq: 7, step: 6, lane: "main" },
    { ...envelope, type: "message", role: "assistant", text: "主线继续", lane: "main", seq: 8 },
    { ...envelope, type: "message", role: "user", text: "告警再次触发", source: "alert", seq: 9 },
  ] satisfies SessionEvent[];
  const { container } = render(<Conversation {...options} events={events} />);
  const rows = [...container.querySelectorAll("[data-aui-thread-root] [data-message-id]")];
  expect(rows.map((row) => row.querySelector("p")?.textContent)).toEqual([
    "尚未领取", "暂等审批时发出", "旁路问", "旁路答", "告警再次触发", "主线继续", "告警再次触发",
  ]);
  expect(rows[0]?.textContent).toContain("已排队 · 等待送达");
  expect(rows[1]?.getAttribute("data-turn")).toBe("main");
  expect(rows[1]?.textContent).toContain("主线 · 已送入 · 第 6 步");
  expect(rows[2]?.getAttribute("data-turn")).toBe("other");
  expect(rows[2]?.textContent).toContain("旁路 · 已送入 · 第 2 步");
});

test("连续调查按轮次和线路归组，迟到结果与重复页保留最早的调用位置", () => {
  const main = { ...envelope, lane: "main" as const };
  const events = [
    query(1, { ...main }),
    { ...main, type: "skill", seq: 2, name: "method", trigger: "signal", status: "failed" },
    { ...main, type: "message", seq: 3, role: "user", text: "补充线索" },
    query(4, { ...main, id: "second", status: "empty", summary: "没有匹配 Pod" }),
    query(5, { ...main, id: "third", lane: "side", status: "error", summary: "连接已超时" }),
    query(6, { ...main, id: "fourth", turn_id: "next-turn", lane: "side", status: "rejected", summary: "本轮预算不足" }),
    { ...main, type: "message", seq: 7, role: "assistant", text: "已收到线索" },
    { ...main, type: "approval_required", seq: 8, card_id: "approval", action: "restart_pod", status: "pending_approval" },
    query(9, { ...main, id: "after-card" }),
    { ...main, type: "result", seq: 10, status: "failed", summary: "调查中断" },
  ] satisfies SessionEvent[];
  const { container, rerender } = render(<Conversation {...options} events={events.slice(0, 3)} />);
  const groups = () => [...container.querySelectorAll(".investigation-process")];
  const originalGroup = groups()[0];
  const originalQuery = originalGroup.querySelector(".observation");
  expect(originalGroup.querySelector(".process-notices")?.textContent).toContain("Skill 读取失败");

  const end = query(11, { ...main, status: "ok", observation: "稍后返回的观测" });
  const full = [...events, end, events[0], events[1]];
  rerender(<Conversation {...options} events={full} />);
  expect(groups()).toHaveLength(5);
  expect(groups()[0]).toBe(originalGroup);
  expect(groups()[0].querySelector(".observation")).toBe(originalQuery);
  expect(originalQuery?.getAttribute("data-status")).toBe("ok");
  expect(groups()[0].querySelector("summary")?.textContent).toContain("1 次查询 · 1 项 Skill");
  expect(groups().slice(1).map((group) => group.querySelector(".process-notices")?.textContent)).toEqual([
    expect.stringContaining("没有匹配 Pod"), expect.stringContaining("连接已超时"),
    expect.stringContaining("本轮预算不足"), expect.stringContaining("查询未完成"),
  ]);
  expect([...container.querySelectorAll(".feed-entry")].map((entry) => entry.firstElementChild?.className))
    .toEqual(["investigation-process", "message user-message", "investigation-process", "investigation-process",
      "investigation-process", "message", "card", "investigation-process", "message"]);
  expect(projectConversation(full).rows).toEqual(projectConversation([...events, end]).rows);
});
