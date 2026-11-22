import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { EventPage, SessionEvent } from "./api";
import { useSessionLedger } from "./session-ledger";

const message = (session_id: string, seq: number): SessionEvent =>
  ({ type: "message", session_id, seq, ts: "2026-11-20T02:14:00Z", role: "user", text: String(seq) });

const json = (value: unknown) => Response.json(value);

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("分页失败后从服务端游标续读，按会话与 seq 去重并按顺序归并", async () => {
  const cursors: number[] = [];
  const mock = vi.fn(async (url: string) => {
    if (!url.includes("/events?")) return json({
      session: { id: "alpha", last_seq: 106 },
      approval_enabled: false,
    });

    const cursor = Number(new URL(url, "http://localhost").searchParams.get("after_seq"));
    cursors.push(cursor);
    if (cursor === 0) {
      return json({
        next_after_seq: 100, has_more: true,
        events: [message("alpha", 4), message("alpha", 4), message("beta", 2), message("alpha", 100)],
      } satisfies EventPage);
    }
    if (cursors.length === 2) throw new Error("连接断开");
    return json({
      next_after_seq: 106, has_more: false,
      events: [message("alpha", 106), message("alpha", 100), message("alpha", 3)],
    } satisfies EventPage);
  });
  vi.stubGlobal("fetch", mock);

  const { result } = renderHook(() => useSessionLedger("alpha"));
  await waitFor(() => expect(result.current.state.error).toBe("连接断开"));
  expect(result.current.state.cursor).toBe(100);
  expect(result.current.state.events.map(({ seq }) => seq)).toEqual([4, 100]);

  act(() => result.current.refresh());
  await waitFor(() => expect(result.current.state.disconnected).toBe(false));
  expect(cursors).toEqual([0, 100, 100]);
  expect(result.current.state.cursor).toBe(106);
  expect(result.current.state.events.map(({ seq }) => seq)).toEqual([3, 4, 100, 106]);
});

test("标题事件与保存回执都能防止迟到快照回滚标题", async () => {
  const late = Promise.withResolvers<Response>();
  const session = {
    id: "alpha", title: "原名", source: "user", state: "idle", last_seq: 1, pending_approval: false,
  } as const;
  let reads = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (!url.includes("/events?")) {
      reads += 1;
      return reads === 2 ? late.promise : json({ session, approval_enabled: true });
    }
    return json({ next_after_seq: 2, has_more: false, events: [{
      ...message("alpha", 2), type: "session", renamed: true, title: "首条问题",
    }] });
  }));
  const { result } = renderHook(() => useSessionLedger("alpha"));
  await waitFor(() => expect(result.current.state.session?.title).toBe("首条问题"));
  act(() => result.current.refresh());
  await waitFor(() => expect(reads).toBe(2));
  act(() => result.current.dispatch({ type: "snapshot", session: { ...session, title: "人工名称", last_seq: 3 } }));
  await act(async () => late.resolve(json({ session, approval_enabled: true })));
  expect(result.current.state.session?.title).toBe("人工名称");
  expect(result.current.state.session?.last_seq).toBe(3);
});
