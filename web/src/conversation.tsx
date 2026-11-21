import { Collapsible } from "@base-ui/react/collapsible";
import { type ReactNode, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Access, CardEvent, CardUpdate, Decision, DeliveryEvent, ObservationEvent, SessionEvent } from "./api";
import { Card } from "./card";
import { Stamp, time } from "./ui";

function Observation({ event, reveal }: { event: ObservationEvent; reveal: object | undefined }) {
  const [open, setOpen] = useState(false);
  const node = useRef<HTMLElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  useLayoutEffect(() => {
    if (!reveal) return;
    setOpen(true);
    node.current?.scrollIntoView({ block: "nearest" });
    trigger.current?.focus({ preventScroll: true });
  }, [reveal]);

  const tool = event.tool?.replaceAll("_", " ") || "查询";
  const details = event.observation;
  const text = details == null ? "观测详情未取到"
    : typeof details === "string" ? details : JSON.stringify(details, null, 2);
  return <Collapsible.Root render={<section ref={node} />} open={open} onOpenChange={setOpen} className="observation">
    <header className="observation-head">
      <Stamp>OBSERVATION · {tool} · {time(event.collected_at, true)}</Stamp>
      <Collapsible.Trigger ref={trigger} className="disclosure" aria-label="展开或收起观测">
        {open ? "[−]" : "[+]"}
      </Collapsible.Trigger>
    </header>
    <p className="observation-summary">{event.summary}</p>
    <Collapsible.Panel keepMounted className={open ? "fold-body open" : "fold-body"} inert={!open}>
      <div><pre>{text}</pre></div>
    </Collapsible.Panel>
  </Collapsible.Root>;
}

function project(events: SessionEvent[]) {
  const deliveries = new Map<string, DeliveryEvent>();
  const observations = new Map<string, number>();
  const evidence = new Map<string, string>();
  const cardUpdates = new Map<string, { update: CardUpdate; expiresAt?: string | null }>();
  const repeatedResults = new Set<number>();
  const replies = new Map<string | undefined | null, Set<string>>();

  for (const event of events) {
    switch (event.type) {
      case "input_delivered":
        deliveries.set(event.input_id, event);
        break;
      case "tool_call":
        if (event.observation_id) observations.set(event.observation_id, event.seq);
        break;
      case "evidence": {
        const id = event.evidence_id ?? event.id;
        if (id && event.observation_id) evidence.set(id, event.observation_id);
        break;
      }
      case "card_decision":
        cardUpdates.set(event.card_id, { update: event, expiresAt: event.status === "approved" ? event.expires_at : undefined });
        break;
      case "action_result":
        cardUpdates.set(event.card_id, { update: event, expiresAt: cardUpdates.get(event.card_id)?.expiresAt });
        break;
      case "message":
        if (event.role === "assistant") {
          const texts = replies.get(event.turn_id) ?? new Set();
          texts.add(event.text);
          replies.set(event.turn_id, texts);
        }
        break;
      case "result":
        if (event.summary && replies.get(event.turn_id)?.has(event.summary)) repeatedResults.add(event.seq);
        break;
    }
  }
  return { deliveries, observations, evidence, cardUpdates, repeatedResults };
}

function Message({ event, delivery }: { event: Extract<SessionEvent, { type: "message" }>; delivery?: DeliveryEvent }) {
  const alert = event.source === "alert";
  const user = event.role === "user" && !alert;
  const hint = delivery?.lane === "side" ? "旁路 · 未送入主线"
    : Number.isInteger(delivery?.step) ? `已送入 · 第 ${delivery?.step} 步` : null;
  return <div className={user ? "message user-message" : "message"}>
    <Stamp>{alert ? "ALERT" : user ? "你" : "AGENT"}</Stamp>
    <p>{event.text}</p>
    {hint && <p className="stamp delivery">{hint}</p>}
  </div>;
}

interface ConversationProps {
  events: SessionEvent[];
  access: Access;
  anchor: string;
  notice: string;
  decide: (card: CardEvent, decision: Decision) => Promise<void>;
}

export function Conversation({ events, access, anchor, notice, decide }: ConversationProps) {
  const projection = useMemo(() => project(events), [events]);
  const [reveal, setReveal] = useState<{ seq: number }>();
  const scroll = useRef<HTMLDivElement>(null);
  const stream = useRef<HTMLDivElement>(null);

  // 只在新事件入账且用户原本在末尾时跟随。折叠操作可改变基准高度。
  const layout = useRef({ height: 0, events });
  useLayoutEffect(() => {
    const node = scroll.current;
    const feed = stream.current;
    if (!node || !feed) return;
    const observer = new ResizeObserver(() => { layout.current.height = node.scrollHeight; });
    observer.observe(feed);
    return () => observer.disconnect();
  }, []);

  useLayoutEffect(() => {
    const node = scroll.current;
    if (!node) return;
    const previous = layout.current;
    if (!anchor && previous.events !== events && previous.events.length &&
      node.scrollHeight > previous.height && previous.height - node.scrollTop - node.clientHeight < 25) {
      node.scrollTop = node.scrollHeight;
    }
    layout.current = { height: node.scrollHeight, events };
  }, [anchor, events]);

  function renderEvent(event: SessionEvent): ReactNode {
    switch (event.type) {
      case "message":
        return <Message event={event} delivery={event.input_id ? projection.deliveries.get(event.input_id) : undefined} />;
      case "tool_call":
        return <Observation event={event} reveal={reveal?.seq === event.seq ? reveal : undefined} />;
      case "hypothesis": {
        const color = event.status === "supported" ? "ok" : event.status === "refuted" ? "danger" : "dim";
        return <p><Stamp>HYPOTHESIS {event.id} · </Stamp>{event.statement} → <Stamp className={color}>{event.status}</Stamp></p>;
      }
      case "approval_required":
        return <Card card={event} access={access} anchored={anchor === `card-${event.card_id}`}
          {...projection.cardUpdates.get(event.card_id)} decide={decide}
          observationFor={(id) => {
            const observation = projection.evidence.get(id);
            return observation ? projection.observations.get(observation) : undefined;
          }}
          reveal={(seq) => setReveal({ seq })} />;
      case "session_closed":
        return <p className="stamp">会话已关闭</p>;
      case "result": {
        if (event.status !== "failed" && event.status !== "budget_exhausted") return null;
        const failed = event.status === "failed";
        return <div className="message">
          <Stamp className={failed ? "danger" : "warn"}>{failed ? "运行失败" : "预算已用完"}</Stamp>
          {event.summary && !projection.repeatedResults.has(event.seq) && <p>{event.summary}</p>}
        </div>;
      }
      default:
        return null;
    }
  }

  let lastMinute: number | undefined;
  return <div id="conversation-scroll" className="conversation-scroll" ref={scroll}>
    <p id="session-notice" className="notice" role="status">{notice}</p>
    <div id="event-stream" className="event-stream" aria-label="会话内容" ref={stream}>
      {events.map((event) => {
        const content = renderEvent(event);
        if (content === null) return null;
        const minute = Math.floor(Date.parse(event.ts) / 60000);
        const showTime = minute !== lastMinute;
        lastMinute = minute;
        return <div className="feed-row" key={event.seq}>
          <div className="feed-entry">{content}</div>
          {showTime && <time dateTime={event.ts}>{time(event.ts)}</time>}
        </div>;
      })}
    </div>
  </div>;
}
