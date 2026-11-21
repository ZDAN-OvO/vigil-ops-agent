import { useCallback, useReducer, useRef } from "react";
import {
  type Access, type EventPage, type Session, type SessionEvent, type SessionResponse,
  errorMessage, isMissing, request, sessionPath,
} from "./api";
import { usePolling } from "./polling";

const readonlyAccess: Access = { approval_enabled: false, approval_disabled_reason: null };

interface LedgerState {
  session: Session | null;
  access: Access;
  events: SessionEvent[];
  cursor: number;
  hasMore: boolean;
  error: string;
  disconnected: boolean;
}

type Update =
  | { type: "snapshot"; session: Session; access?: Access }
  | { type: "page"; id: string; page: EventPage }
  | { type: "status"; error: string; disconnected: boolean };

const initial: LedgerState = {
  session: null, access: readonlyAccess, events: [], cursor: 0, hasMore: false, error: "", disconnected: false,
};

function reduce(state: LedgerState, update: Update): LedgerState {
  switch (update.type) {
    case "snapshot": {
      const session = state.session && state.session.last_seq > update.session.last_seq
        ? state.session : update.session;
      return { ...state, session, access: update.access ?? state.access };
    }
    case "page": {
      const { page, id } = update;
      const seen = new Set(state.events.map((event) => event.seq));
      const added = page.events.filter((event) => {
        if (event.session_id !== id || !Number.isSafeInteger(event.seq) || seen.has(event.seq)) return false;
        seen.add(event.seq);
        return true;
      });
      const events = added.length
        ? [...state.events, ...added].sort((a, b) => a.seq - b.seq)
        : state.events;
      return { ...state, cursor: page.next_after_seq, hasMore: page.has_more, events };
    }
    case "status":
      return { ...state, error: update.error, disconnected: update.disconnected };
  }
}

// 调用方以会话 ID 作为组件 key。续读位置和在途请求随该会话视图销毁。
export function useSessionLedger(id: string | undefined) {
  const [state, dispatch] = useReducer(reduce, initial);
  const cursor = useRef(0);
  const api = id ? `/api${sessionPath(id)}` : null;
  const poll = useCallback(async (signal: AbortSignal) => {
    if (!api || !id) return;
    try {
      const detail = await request<SessionResponse>(api, { signal });
      if (signal.aborted) return;
      if (detail.session.id !== id) throw new Error("会话响应与当前会话不符");
      dispatch({ type: "snapshot", session: detail.session, access: detail });

      let page: EventPage;
      do {
        const previous = cursor.current;
        page = await request<EventPage>(`${api}/events?after_seq=${previous}&limit=100`, { signal });
        if (signal.aborted) return;
        const next = page.next_after_seq;
        if (!Number.isSafeInteger(next) || next < previous || (page.has_more && next === previous)) {
          throw new Error("事件账本续读位置无效");
        }
        dispatch({ type: "page", id, page });
        cursor.current = next;
      } while (page.has_more);

      dispatch({ type: "status", error: "", disconnected: false });
    } catch (error) {
      if (signal.aborted) return;
      dispatch({
        type: "status",
        error: isMissing(error) ? "会话不存在" : errorMessage(error),
        disconnected: !isMissing(error),
      });
    }
  }, [api, id]);

  return { state, refresh: usePolling(poll, 900), dispatch };
}
