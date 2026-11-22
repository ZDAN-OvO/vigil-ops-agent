import {
  AssistantRuntimeProvider, MessagePrimitive, ThreadPrimitive, type TextMessagePartProps,
  type ThreadMessageLike, useExternalStoreRuntime,
} from "@assistant-ui/react";
import { type ComponentProps, useLayoutEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Access, CardEvent, Decision, DeliveryEvent, SessionEvent } from "./api";
import { Card } from "./card";
import { type ConversationRow, type MessageEvent, projectConversation } from "./conversation-projection";
import { Evidence, HypothesisDetails, type ObservationJump, ProcessDetails } from "./investigation-details";
import { Stamp, time } from "./ui";

function linkUrl(value: string): string {
  try {
    const url = new URL(value);
    if ((url.protocol === "https:" || url.protocol === "http:") && !url.username && !url.password) {
      return url.href;
    }
  } catch {
    // 只有有效的 HTTP(S) 目的地址可作为链接。
  }
  return "";
}

const markdownComponents: ComponentProps<typeof Markdown>["components"] = {
  a: ({ href, children }) => href
    ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
    : <>{children}</>,
  img: ({ alt }) => <>{alt}</>,
  pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
  table: ({ children }) => <div className="markdown-table" tabIndex={0}><table>{children}</table></div>,
  th: ({ children }) => <th>{children}</th>,
  td: ({ children }) => <td>{children}</td>,
};

function PlainText({ text }: TextMessagePartProps) {
  return <p>{text}</p>;
}

function AssistantText({ text }: TextMessagePartProps) {
  return <div className="assistant-markdown">
    <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={linkUrl} components={markdownComponents}>
      {text}
    </Markdown>
  </div>;
}

const plainParts = { Text: PlainText };
const assistantParts = { Text: AssistantText };

function Message({ event, delivery }: { event: MessageEvent; delivery?: DeliveryEvent }) {
  const alert = event.source === "alert";
  const user = event.role === "user" && !alert;
  const lane = delivery?.lane === "side" ? "旁路" : "主线";
  const hint = delivery
    ? [lane, Number.isInteger(delivery.step) ? `已送入 · 第 ${delivery.step} 步` : "已送入"].join(" · ")
    : user && event.input_id ? "已排队 · 等待送达" : null;
  return <MessagePrimitive.Root className={user ? "message user-message" : "message"}
    data-turn={delivery?.turn_id ?? event.turn_id ?? undefined} data-lane={delivery?.lane ?? event.lane}>
    <Stamp>{alert ? "告警" : user ? "你" : event.lane === "side" ? "助手 · 旁路" : "助手"}</Stamp>
    <MessagePrimitive.Parts components={event.role === "assistant" && !alert ? assistantParts : plainParts} />
    {hint && <p className="stamp delivery">{hint}</p>}
  </MessagePrimitive.Root>;
}

function convertMessage(entry: ConversationRow): ThreadMessageLike {
  const { key } = entry;
  const event = "row" in entry ? entry.row.event : undefined;
  if (event?.type === "message") {
    return {
      id: key,
      role: event.role === "user" && event.source !== "alert" ? "user" : "assistant",
      content: event.text,
    };
  }
  return { id: key, role: "assistant", content: [] };
}

// 调查输入由工作区发送。会话运行时只消费已经入账的内容。
const onNew = async () => {};

interface ConversationProps {
  events: SessionEvent[];
  access: Access;
  anchor: string;
  notice: string;
  decide: (card: CardEvent, decision: Decision) => Promise<void>;
}

export function Conversation({ events, access, anchor, notice, decide }: ConversationProps) {
  const projection = useMemo(() => projectConversation(events), [events]);
  const rows = useMemo(() => new Map(projection.rows.map((row) => [row.key, row])), [projection]);
  const runtime = useExternalStoreRuntime({
    messages: projection.rows, convertMessage, onNew, isSendDisabled: true,
  });
  const [jump, setJump] = useState<ObservationJump>();
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

  function renderRow(entry: ConversationRow) {
    if ("process" in entry) {
      const target = jump && entry.process.items.some((item) => item.key === jump.key) ? jump : undefined;
      return <ProcessDetails process={entry.process} jump={target} dismissJump={() => setJump(undefined)} />;
    }
    const { row } = entry;
    const { event } = row;
    switch (event.type) {
      case "message":
        return <Message event={event} delivery={row.delivery} />;
      case "hypothesis":
        return row.hypothesis && <HypothesisDetails hypothesis={row.hypothesis}
          observationFor={projection.observationFor} reveal={setJump} />;
      case "evidence":
        return <Evidence event={event} observationFor={projection.observationFor} reveal={setJump} />;
      case "approval_required":
        return <Card card={event} access={access} anchored={anchor === `card-${event.card_id}`}
          {...projection.cardUpdates.get(event.card_id)} decide={decide}
          observationFor={(id) => projection.cardObservationFor(event, id)} reveal={setJump} />;
      case "session_closed":
        return <p className="stamp">会话已关闭</p>;
      case "result": {
        const failed = event.status === "failed";
        return <div className="message">
          <Stamp className={failed ? "danger" : "warn"}>{failed ? "运行失败" : "预算已用完"}</Stamp>
          {event.summary && !row.repeatedSummary && <p>{event.summary}</p>}
        </div>;
      }
      default:
        return null;
    }
  }

  return <AssistantRuntimeProvider runtime={runtime}>
    <ThreadPrimitive.Root id="conversation-scroll" className="conversation-scroll" ref={scroll}>
      <p id="session-notice" className="notice" role="status">{notice}</p>
      <div id="event-stream" className="event-stream" aria-label="会话内容" ref={stream}>
        <ThreadPrimitive.Messages>
          {({ message }) => {
            const row = rows.get(message.id);
            if (!row) return null;
            const at = row.ts;
            return <div className="feed-row">
              <div className="feed-entry">{renderRow(row)}</div>
              {row.showTime && <time dateTime={at}>{time(at)}</time>}
            </div>;
          }}
        </ThreadPrimitive.Messages>
      </div>
    </ThreadPrimitive.Root>
  </AssistantRuntimeProvider>;
}
