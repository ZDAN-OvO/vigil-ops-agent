import { Popover } from "@base-ui/react/popover";
import { type FocusEvent, type KeyboardEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { SessionEvent, SessionState } from "./api";
import { Spinner, Stamp } from "./ui";

function summary(event: SessionEvent) {
  switch (event.type) {
    case "session": return "会话开始或恢复";
    case "session_closed": return "会话关闭";
    case "message": return event.source === "alert" ? "告警消息" : event.role === "user" ? "调查问题" : "Agent 回复";
    case "tool_call": return event.summary || "工具调用";
    case "hypothesis": return "更新假设";
    case "evidence": return "关联证据";
    case "input_delivered": return event.lane === "side" ? "旁路接收" : "调查提示送达";
    case "approval_required": return "等待人工审批";
    case "card_decision": return event.status === "approved" ? "批准决定" : "拒绝决定";
    case "action_result": return "动作回执";
    case "result": return "轮次结果";
    default: return (event as { type: string }).type;
  }
}

const triggerId = (seq: number) => `ledger-event-${seq}`;

export function Ledger({ events, state }: { events: SessionEvent[]; state?: SessionState }) {
  const root = useRef<HTMLDivElement>(null);
  const popup = useRef<HTMLPreElement>(null);
  const focusPopupOnMount = useRef(false);
  const attachPopup = useCallback((node: HTMLPreElement | null) => {
    popup.current = node;
    if (node && focusPopupOnMount.current) {
      focusPopupOnMount.current = false;
      node.focus({ preventScroll: true });
    }
  }, []);
  const [capacity, setCapacity] = useState(3);
  const [historyEnd, setHistoryEnd] = useState<number>();
  const [inspected, setInspected] = useState<number>();
  const dismissed = useRef<number | undefined>(undefined);
  const focused = useRef<HTMLElement | null>(null);
  const gap = state === "awaiting_approval";
  const latest = events.at(-1);
  const historySlots = Math.max(1, capacity - 1 - Number(gap));
  const tail = Math.max(0, events.length - 1);
  const earliest = Math.min(historySlots, tail);
  const index = historyEnd === undefined ? tail : events.findIndex((event) => event.seq >= historyEnd);
  const end = Math.max(earliest, Math.min(index < 0 ? tail : index, tail));
  const view = latest ? [...events.slice(end - earliest, end), latest] : [];
  const current = view.find((event) => event.seq === inspected);

  useLayoutEffect(() => {
    const node = root.current;
    if (!node) return;
    const resize = () => {
      if (node.clientWidth) setCapacity(Math.max(3, Math.floor((node.clientWidth - 70) / 115)));
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(node);
    return () => observer.disconnect();
  }, [!!latest]);

  useLayoutEffect(() => {
    if (current === undefined && inspected !== undefined) setInspected(undefined);
    const previouslyFocused = focused.current;
    if (!previouslyFocused || document.activeElement !== document.body) return;
    if (previouslyFocused.matches(".ledger-prev:disabled")) {
      root.current?.focus({ preventScroll: true });
    } else if (!previouslyFocused.isConnected ||
      (previouslyFocused.matches(".event-popover") && !current)) {
      const fallback = view[0];
      if (fallback) document.getElementById(triggerId(fallback.seq))?.focus({ preventScroll: true });
    }
  });

  function close(returnFocus = false) {
    if (returnFocus && inspected !== undefined) {
      dismissed.current = inspected;
      document.getElementById(triggerId(inspected))?.focus({ preventScroll: true });
    }
    setInspected(undefined);
  }

  useEffect(() => {
    if (!current) return;
    // 悬停也能打开浮层，焦点在账本外时同样响应 Esc。
    function escape(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      dismissed.current = current?.seq;
      setInspected(undefined);
      if (current) document.getElementById(triggerId(current.seq))?.focus({ preventScroll: true });
    }
    document.addEventListener("keydown", escape, true);
    return () => document.removeEventListener("keydown", escape, true);
  }, [current]);

  function shift(delta: number) {
    const next = Math.max(earliest, Math.min(tail, end + delta));
    if (next !== end) setHistoryEnd(next === tail ? undefined : events[next].seq);
  }

  function settle() {
    queueMicrotask(() => {
      setInspected((active) => {
        if (active === undefined) return active;
        const trigger = document.getElementById(triggerId(active));
        const over = trigger?.matches(":hover") || popup.current?.matches(":hover");
        const within = trigger === document.activeElement || popup.current?.contains(document.activeElement);
        return over || within ? active : undefined;
      });
    });
  }

  function trackFocus(event: FocusEvent<HTMLElement>) {
    if (event.target instanceof HTMLElement) focused.current = event.target;
  }

  function trackBlur(event: FocusEvent<HTMLElement>) {
    if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) focused.current = null;
  }

  function keyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.target === popup.current || event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      shift(event.key === "ArrowLeft" ? -1 : 1);
    }
  }

  return <Popover.Root open={!!current} triggerId={current ? triggerId(current.seq) : null}
    onOpenChange={(open, details) => {
      if (!open && details.reason === "outside-press") close();
    }}>
    <div id="ledger" className="ledger" aria-label="会话事件账本" role="region" tabIndex={0} hidden={!latest}
      ref={root} onKeyDown={keyboard} onFocus={trackFocus} onBlur={trackBlur}>
      <button type="button" className="ledger-prev" aria-label="查看更早的事件" hidden={tail === earliest}
        disabled={end === earliest} onClick={() => shift(-1)}>←</button>
      <div className="ledger-window">
        {Array.from({ length: capacity - view.length - Number(gap) }, (_, index) =>
          <span key={`empty-${index}`} aria-hidden="true" />)}
        {view.map((event) => <Popover.Trigger key={event.seq} id={triggerId(event.seq)}
          className={event === latest ? "ledger-cell current" : "ledger-cell"}
          data-seq={event.seq} aria-current={event === latest || undefined}
          onPointerEnter={(pointer) => {
            if (pointer.pointerType === "touch") return;
            dismissed.current = undefined;
            setInspected(event.seq);
          }}
          onPointerLeave={settle}
          onFocus={() => { if (dismissed.current !== event.seq) setInspected(event.seq); }}
          onBlur={() => { if (dismissed.current === event.seq) dismissed.current = undefined; settle(); }}
          onClick={() => {
            dismissed.current = undefined;
            setInspected(event.seq);
            if (popup.current) popup.current.focus({ preventScroll: true });
            else focusPopupOnMount.current = true;
          }}
          onKeyDown={(key) => {
            if (key.key === "Tab" && !key.shiftKey && current?.seq === event.seq && popup.current) {
              key.preventDefault();
              popup.current.focus({ preventScroll: true });
            }
          }}>
          <Stamp>SEQ {String(event.seq).padStart(4, "0")}</Stamp>
          <span className="ledger-summary"><span>{summary(event)}</span>
            <Spinner hidden={event !== latest || state !== "working"} />
          </span>
        </Popover.Trigger>)}
        {gap && <span className="ledger-cell ledger-next mono" aria-label="下一步等待审批">— — —</span>}
      </div>
    </div>
    <Popover.Portal>
      <Popover.Positioner className="event-positioner" positionMethod="fixed" side="bottom" align="start"
        collisionPadding={8} collisionAvoidance={{ side: "flip", align: "flip", fallbackAxisSide: "none" }}>
        <Popover.Popup render={<pre ref={attachPopup} />} className="event-popover" aria-label="会话事件详情"
          tabIndex={0} initialFocus={false} finalFocus={false} onFocus={trackFocus}
          onBlur={settle} onPointerLeave={settle}
          onKeyDown={(event) => {
            if (event.key !== "Tab" || !current) return;
            if (event.shiftKey) {
              event.preventDefault();
              document.getElementById(triggerId(current.seq))?.focus({ preventScroll: true });
            } else {
              // 从触发格继续浏览器原有的 Tab 顺序。
              close(true);
            }
          }}>
          {current && JSON.stringify(current, null, 2)}
        </Popover.Popup>
      </Popover.Positioner>
    </Popover.Portal>
  </Popover.Root>;
}
