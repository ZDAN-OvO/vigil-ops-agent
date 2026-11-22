import { type FormEvent, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, Outlet, useLocation, useMatch, useNavigate, useOutletContext, useParams } from "react-router";
import type { CardEvent, Decision, Session, SessionsResponse } from "./api";
import { errorMessage, request, sessionPath } from "./api";
import { Conversation } from "./conversation";
import { Monitoring } from "./monitoring";
import { usePolling } from "./polling";
import { mergeSession, useSessionLedger } from "./session-ledger";
import { SessionTitle } from "./session-title";
import { WorkspaceLayout } from "./workspace-layout";
import { sessionStates, Spinner, Stamp } from "./ui";

interface SessionControls {
  ask: (text: string) => void;
  resumed: (session: Session) => void;
}

interface MessageAttempt {
  input_id: string;
  sending: boolean;
}

interface WorkspaceContext {
  connection: HTMLSpanElement | null;
  sessionControls: React.RefObject<SessionControls | null>;
  attempts: Map<string, Map<string, MessageAttempt>>;
  decisions: Map<string, Promise<unknown>>;
  refreshList: () => void;
  rememberSession: (session: Session) => void;
  known?: Session;
}

export function Workspace() {
  const activeId = useMatch("/sessions/:sessionId")?.params.sessionId;
  const navigate = useNavigate();
  const location = useLocation();
  const locationKey = useRef(location.key);
  useLayoutEffect(() => { locationKey.current = location.key; }, [location.key]);

  const [list, setList] = useState<SessionsResponse>();
  const [listError, setListError] = useState("");
  const [listNotice, setListNotice] = useState("");
  const [pending, setPending] = useState<{ type: "create" } | { type: "resume"; id: string }>();
  const pendingRef = useRef(false);
  const knownSessions = useRef(new Map<string, Session>());
  const sessionControls = useRef<SessionControls>(null);
  const [connection, setConnection] = useState<HTMLSpanElement | null>(null);
  const attempts = useRef<WorkspaceContext["attempts"]>(new Map());
  const decisions = useRef<WorkspaceContext["decisions"]>(new Map());
  const poll = useCallback(async (signal: AbortSignal) => {
    try {
      const result = await request<SessionsResponse>("/api/sessions", { signal });
      if (!signal.aborted) {
        const sessions = result.sessions.map((item) => {
          const latest = mergeSession(knownSessions.current.get(item.id), item);
          knownSessions.current.set(item.id, latest);
          return latest;
        });
        setList({ ...result, sessions });
        setListError("");
      }
    } catch (error) {
      if (!signal.aborted) setListError(errorMessage(error));
    }
  }, []);
  const refreshList = usePolling(poll, 3000);
  const rememberSession = useCallback((session: Session) => {
    if ((knownSessions.current.get(session.id)?.last_seq ?? -1) >= session.last_seq) return;
    knownSessions.current.set(session.id, session);
    setList((current) => current && {
      ...current,
      sessions: current.sessions.map((item) => item.id === session.id ? mergeSession(item, session) : item),
    });
  }, []);

  async function changeSession(id?: string) {
    if (pendingRef.current || !list?.approval_enabled) return;
    pendingRef.current = true;
    setPending(id ? { type: "resume", id } : { type: "create" });
    setListNotice(id ? "正在恢复…" : "正在创建会话…");
    const startedAt = location.key;
    try {
      const path = id ? `/api${sessionPath(id)}/resume` : "/api/sessions";
      const response = await request<{ session: Session }>(path, { method: "POST" });
      setListNotice("");
      rememberSession(response.session);
      refreshList();
      if (startedAt !== locationKey.current) return;
      if (id === activeId && id) sessionControls.current?.resumed(response.session);
      else void navigate(sessionPath(response.session.id));
    } catch (error) {
      setListNotice(errorMessage(error));
    } finally {
      pendingRef.current = false;
      setPending(undefined);
    }
  }

  return <>
    <header className="topbar">
      <Link className="brand" to="/">Vigil</Link>
      <span id="connection" className="stamp" role="status" ref={setConnection} />
    </header>
    <WorkspaceLayout sessions={(closeOverlay) => <aside className="sessions" aria-labelledby="sessions-title">
        <header className="sessions-head">
          <h2 id="sessions-title" className="stamp">会话</h2>
          <button className="session-control" id="new-session" type="button"
            disabled={!list?.approval_enabled || pending?.type === "create"}
            onClick={() => { closeOverlay(); void changeSession(); }}>新会话</button>
        </header>
        <p id="sessions-access" className="notice">
          {list && !list.approval_enabled && `只读 · ${list.approval_disabled_reason || "当前无法创建会话"}`}
        </p>
        <p id="sessions-notice" className="notice" role="status">
          {listNotice || listError || (list?.sessions.length === 0 ? "暂无会话" : "")}
        </p>
        <nav id="session-list" aria-label="会话列表">
          {list?.sessions.map((item) => {
            const selected = item.id === activeId;
            return <div className={selected ? "session-row selected" : "session-row"} key={item.id}>
              {item.pending_approval && <span className="flag" aria-label="有待审批卡片" />}
              <Link className="session-link" to={sessionPath(item.id)} aria-current={selected ? "page" : undefined}
                onClick={closeOverlay}>
                <span className="session-name">{item.title || item.id}</span>
                <Stamp className="stamp-small">{item.source === "alert" ? "告警" : "人工"}</Stamp>
                <Stamp className={item.state === "working" ? "stamp-small gold" : "stamp-small"}>
                  {sessionStates[item.state] ?? "状态未取到"}
                </Stamp>
              </Link>
              {item.state === "interrupted" && <button type="button" className="stamp stamp-small session-resume"
                disabled={!list.approval_enabled || (pending?.type === "resume" && pending.id === item.id)}
                onClick={() => { closeOverlay(); void changeSession(item.id); }}>接手恢复</button>}
              {item.state === "working" && <Spinner />}
            </div>;
          })}
        </nav>
      </aside>} monitoring={(collapsed, toggle, closeOverlay) => <Monitoring collapsed={collapsed} toggle={toggle} openSession={closeOverlay}
        ask={(text) => { closeOverlay(); sessionControls.current?.ask(text); }} />}>
      <Outlet context={{
        connection, sessionControls, attempts: attempts.current, decisions: decisions.current, refreshList,
        rememberSession, known: activeId ? knownSessions.current.get(activeId) : undefined,
      } satisfies WorkspaceContext} />
    </WorkspaceLayout>
  </>;
}

export function SessionRoute() {
  const { sessionId } = useParams();
  const context = useOutletContext<WorkspaceContext>();
  return <ActiveSession key={sessionId ?? ""} id={sessionId} {...context} />;
}

function cardAnchor(hash: string) {
  let fragment = hash.slice(1);
  try { fragment = decodeURIComponent(fragment); } catch { /* 保留原片段供缺失提示使用。 */ }
  return fragment.startsWith("card-") ? fragment : "";
}

function ActiveSession({ id, connection, sessionControls, attempts, decisions, refreshList, rememberSession, known }:
  WorkspaceContext & { id?: string }) {
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState("");
  const [sending, setSending] = useState(false);
  const [queued, setQueued] = useState<string>();
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const question = useRef<HTMLTextAreaElement>(null);
  const { hash } = useLocation();
  const anchor = cardAnchor(hash);
  const { state, refresh, dispatch } = useSessionLedger(id, known);
  const { session, access, events } = state;
  useEffect(() => { if (session) rememberSession(session); }, [session, rememberSession]);
  const closed = session?.state === "closed";
  const api = id ? `/api${sessionPath(id)}` : null;
  const ready = !!session && !state.hasMore && state.cursor >= session.last_seq;
  const anchorMissing = anchor && ready && !events.some((event) =>
    event.type === "approval_required" && `card-${event.card_id}` === anchor);
  const sendDisabled = !session || closed || !access.approval_enabled || !draft.trim() || sending;

  useImperativeHandle(sessionControls, () => ({
    ask(text) {
      setDraft(text);
      setQueued(undefined);
      setNotice(!session ? "选择会话后可发送" : closed ? "此会话已关闭"
        : !access.approval_enabled ? "只读 · 当前无法发送调查问题" : "");
      question.current?.focus();
    },
    resumed(session) {
      if (session.id !== id) return;
      dispatch({ type: "snapshot", session });
      setQueued(undefined);
      setNotice("已提交恢复");
      refresh();
    },
  }), [session, closed, access.approval_enabled, id, dispatch, refresh]);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sendDisabled || !id || !api) return;
    const text = draft.trim();
    const sessionAttempts = attempts.get(id) ?? new Map<string, MessageAttempt>();
    const attempt = sessionAttempts.get(text) ?? { input_id: crypto.randomUUID(), sending: false };
    if (attempt.sending) return;
    // 服务可能已入账。同一会话中，同一未确认草稿的重试使用原输入 ID。
    attempts.set(id, sessionAttempts);
    sessionAttempts.set(text, attempt);
    attempt.sending = true;
    setSending(true);
    setQueued(undefined);
    setNotice("正在投递…");
    try {
      const response = await request<{ input_id: string }>(`${api}/messages`, {
        method: "POST", body: { text, input_id: attempt.input_id },
      });
      sessionAttempts.delete(text);
      if (!sessionAttempts.size) attempts.delete(id);
      setDraft((current) => current.trim() === text ? "" : current);
      setQueued(response.input_id);
      setNotice("已排队 · 等待账本更新");
      refresh();
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      attempt.sending = false;
      setSending(false);
    }
  }

  async function close() {
    if (closingRef.current || !api) return;
    closingRef.current = true;
    setClosing(true);
    setQueued(undefined);
    setNotice("正在关闭…");
    try {
      const { session } = await request<{ session: Session }>(`${api}/close`, { method: "POST" });
      if (session.id !== id) throw new Error("会话响应与当前会话不符");
      dispatch({ type: "snapshot", session });
      setNotice("会话已关闭");
      refresh();
      refreshList();
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      closingRef.current = false;
      setClosing(false);
    }
  }

  async function saveTitle(title: string) {
    const { session } = await request<{ session: Session }>(`${api}/title`, { method: "POST", body: { title } });
    if (session.id !== id) throw new Error("会话响应与当前会话不符");
    dispatch({ type: "snapshot", session });
    rememberSession(session);
    refresh();
    refreshList();
  }

  function decisionPath(cardId: string) {
    return `${api}/cards/${encodeURIComponent(cardId)}/decision`;
  }

  useEffect(() => {
    if (!decisions.size) return;
    for (const event of events) {
      if (event.type === "card_decision" || event.type === "action_result") {
        decisions.delete(decisionPath(event.card_id));
      }
    }
  }, [api, decisions, events]);

  async function decide(card: CardEvent, decision: Decision) {
    const path = decisionPath(card.card_id);
    let submission = decisions.get(path);
    if (!submission) {
      // 离开并返回同一会话时，仍复用尚未在账本确认的审批请求。
      submission = request(path, { method: "POST", body: { fingerprint: card.fingerprint, decision } })
        .catch((error: unknown) => {
          decisions.delete(path);
          throw error;
        });
      decisions.set(path, submission);
    }
    await submission;
    refresh();
    refreshList();
  }

  const composeNotice = queued && events.some((event) => event.type === "message" && event.input_id === queued)
    ? "" : notice;

  return <section className="conversation" aria-labelledby="session-title">
    {connection && createPortal(state.disconnected ? "连接中断 · 正在重试" : "", connection)}
    <header className="conversation-head">
      <div className="session-heading">
        <p id="session-meta" className="stamp" data-state={session?.state}>{session && <>
          {`SESSION ${session.id} · ${session.source === "alert" ? "告警" : "人工"} · `}
          <span className={session.state === "awaiting_approval" ? "warn" : undefined}>
            {sessionStates[session.state] ?? "状态未取到"}
          </span>
        </>}</p>
        <SessionTitle session={session} canWrite={access.approval_enabled} save={saveTitle} />
      </div>
      <div id="session-actions">
        {session?.source === "alert" && !closed && <button className="session-control" type="button"
          disabled={closing || !access.approval_enabled} onClick={() => void close()}>关闭告警会话</button>}
        {session && !access.approval_enabled && <Stamp>{access.approval_disabled_reason || "只读"}</Stamp>}
      </div>
    </header>
    <Conversation events={events} access={access} anchor={anchor} decide={decide}
      notice={state.error || (anchorMissing ? "此会话中找不到链接指向的卡片" : "")} />
    <form id="composer" className="composer" onSubmit={(event) => void send(event)}>
      <p id="compose-notice" className="notice" role="status">{composeNotice}</p>
      <div className="compose-row">
        <label className="visually-hidden" htmlFor="question">输入调查问题</label>
        <textarea id="question" rows={2} placeholder="输入调查问题…" ref={question}
          value={draft} onChange={(event) => setDraft(event.currentTarget.value)} />
        <button id="send" type="submit" disabled={sendDisabled}>发送</button>
      </div>
    </form>
  </section>;
}
