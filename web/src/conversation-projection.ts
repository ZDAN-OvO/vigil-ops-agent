import type {
  CardEvent, CardUpdate, DeliveryEvent, InspectionSummaryEvent, ObservationEvent, RemediationProgress, SessionEvent,
} from "./api";

export type MessageEvent = Extract<SessionEvent, { type: "message" }>;
export type SkillEvent = Extract<SessionEvent, { type: "skill" }>;
type HypothesisEvent = Extract<SessionEvent, { type: "hypothesis" }>;
export type EvidenceEvent = Extract<SessionEvent, { type: "evidence" }>;

interface HypothesisVersion {
  change: HypothesisEvent;
  evidence: EvidenceEvent[];
}

export interface Hypothesis {
  latest: HypothesisEvent;
  evidence: Map<string, EvidenceEvent>;
  history: HypothesisVersion[];
}

interface Row {
  key: string;
  event: SessionEvent;
  delivery?: DeliveryEvent;
  hypothesis?: Hypothesis;
  repeatedSummary?: boolean;
  firstTs?: string;
  processScope?: string;
  inspectionHistory?: InspectionSummaryEvent[];
}

interface ProcessItem {
  key: string;
  event: ObservationEvent | SkillEvent;
  finished?: boolean;
}

export interface InvestigationProcess {
  key: string;
  scope?: string;
  lane?: string;
  items: ProcessItem[];
}

export type ConversationRow = { key: string; ts: string; showTime: boolean } &
  ({ row: Row } | { process: InvestigationProcess });

function scoped(event: SessionEvent, id: string): string {
  return JSON.stringify([event.session_id, event.turn_id ?? null, id]);
}

function processScope(event: ObservationEvent | SkillEvent): string | undefined {
  return event.turn_id ? scoped(event, event.lane ?? "") : undefined;
}

function hypothesisKey(event: SessionEvent, id: string): string {
  return event.turn_id && id ? scoped(event, id) : JSON.stringify([event.session_id, event.seq]);
}

export function projectConversation(events: readonly SessionEvent[]) {
  const rows = new Map<string, Row>();
  const seen = new Set<string>();
  const deliveries = new Map<string, DeliveryEvent>();
  const observations = new Map<string, string>();
  const evidenceRefs = new Map<string, string>();
  const hypotheses = new Map<string, Hypothesis>();
  const references = new Map<string, Map<string, EvidenceEvent>>();
  const cardUpdates = new Map<string, { update: CardUpdate; expiresAt?: string | null }>();
  const remediationProgress = new Map<string, RemediationProgress>();
  const assistantTexts = new Set<string>();
  const interimReplies = new Map<string, number>();
  const completedTurns = new Set<string>();
  const closed = new Set<string>();
  const inspectionRows = new Map<string, Row>();

  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const key = JSON.stringify([event.session_id, event.seq]);
    if (seen.has(key)) continue;
    seen.add(key);
    switch (event.type) {
      case "input_delivered":
        deliveries.set(JSON.stringify([event.session_id, event.input_id]), event);
        break;
      case "tool_call": {
        const call = event.id && event.turn_id ? scoped(event, event.id) : key;
        const row = rows.get(call);
        // 终结结果优先。重放的开始事件不能让查询重新进入运行中。
        if (!row) rows.set(call, { key: call, event, firstTs: event.ts, processScope: processScope(event) });
        else if (row.event.type === "tool_call" &&
          (row.event.status === "running" || event.status !== "running")) {
          row.event = { ...event, lane: event.lane ?? row.event.lane,
            query_scope: event.query_scope ?? row.event.query_scope };
        }
        if (event.observation_id) observations.set(scoped(event, event.observation_id), call);
        break;
      }
      case "evidence": {
        const id = event.evidence_id ?? event.id;
        if (id && event.observation_id) evidenceRefs.set(scoped(event, id), event.observation_id);
        if (!event.relation && !event.rationale) break;
        if (!event.hypothesis_id || !event.turn_id) {
          rows.set(key, { key, event });
          break;
        }
        const hyp = hypothesisKey(event, event.hypothesis_id);
        const links = references.get(hyp) ?? new Map();
        links.set(event.observation_id ?? id ?? key, event);
        references.set(hyp, links);
        // 历史事件里可能没有对应假设，先保留独立条目。
        rows.set(key, { key, event });
        break;
      }
      case "hypothesis": {
        const hyp = hypothesisKey(event, event.id);
        const links = references.get(hyp) ?? new Map();
        references.set(hyp, links);
        const version = { change: event, evidence: [...links.values()] };
        const current = hypotheses.get(hyp);
        if (current) {
          current.latest = event;
          current.history.push(version);
        } else {
          const hypothesis = { latest: event, evidence: links, history: [version] };
          hypotheses.set(hyp, hypothesis);
          rows.set(key, { key, event, hypothesis });
        }
        break;
      }
      case "skill":
        rows.set(key, { key, event, processScope: processScope(event) });
        break;
      case "approval_required":
        rows.set(key, { key, event });
        break;
      case "card_decision":
        cardUpdates.set(event.card_id, { update: event, expiresAt: event.status === "approved" ? event.expires_at : undefined });
        break;
      case "action_result":
        cardUpdates.set(event.card_id, { update: event, expiresAt: cardUpdates.get(event.card_id)?.expiresAt });
        break;
      case "remediation_progress":
        remediationProgress.set(event.card_id, event);
        break;
      case "message": {
        if (event.role === "assistant") {
          assistantTexts.add(scoped(event, event.text));
          if (event.turn_id) {
            const reply = scoped(event, JSON.stringify([event.lane, event.text]));
            const interimCount = interimReplies.get(reply) ?? 0;
            if (event.interim) interimReplies.set(reply, interimCount + 1);
            else if (interimCount) {
              interimReplies.set(reply, interimCount - 1);
              break;
            }
          }
        }
        rows.set(key, { key, event });
        break;
      }
      case "result":
        if (event.turn_id && event.status !== "awaiting_approval") {
          completedTurns.add(scoped(event, ""));
        }
        if (event.status === "failed" || event.status === "budget_exhausted") {
          const repeatedSummary = !!event.turn_id && !!event.summary && assistantTexts.has(scoped(event, event.summary));
          rows.set(key, { key, event, repeatedSummary });
        }
        break;
      case "inspection_summary": {
        const current = inspectionRows.get(event.session_id);
        if (current) {
          current.inspectionHistory?.push(event);
          current.event = event;
        } else {
          const row = { key, event, firstTs: event.ts, inspectionHistory: [event] };
          inspectionRows.set(event.session_id, row);
          rows.set(key, row);
        }
        break;
      }
      case "session_closed":
        closed.add(event.session_id);
        rows.set(key, { key, event });
        break;
    }
  }

  let lastMinute: number | undefined;
  const visible: ConversationRow[] = [];
  let process: InvestigationProcess | undefined;
  function append(entry: { row: Row } | { process: InvestigationProcess }, key: string, ts: string) {
    const minute = Math.floor(Date.parse(ts) / 60000);
    visible.push({ ...entry, key, ts, showTime: minute !== lastMinute });
    lastMinute = minute;
  }
  for (const row of rows.values()) {
    const { event } = row;
    if (event.type === "tool_call" || event.type === "skill") {
      if (!process || !row.processScope || process.scope !== row.processScope) {
        process = { key: row.key, scope: row.processScope, lane: event.lane, items: [] };
        append({ process }, process.key, row.firstTs ?? event.ts);
      }
      process.items.push({
        key: row.key, event,
        finished: closed.has(event.session_id) || completedTurns.has(scoped(event, "")),
      });
      continue;
    }
    // 历史依据先独立出现，后续可能并入假设。它的边界仍保留，避免移动已展开的查询。
    process = undefined;
    if (event.type === "evidence" && event.hypothesis_id && event.turn_id &&
      hypotheses.has(hypothesisKey(event, event.hypothesis_id))) continue;
    if (event.type === "message" && event.input_id) {
      row.delivery = deliveries.get(JSON.stringify([event.session_id, event.input_id]));
    }
    append({ row }, row.key, row.firstTs ?? event.ts);
  }
  return {
    rows: visible,
    cardUpdates,
    remediationProgress,
    observationFor: (event: EvidenceEvent, id: string) => observations.get(scoped(event, id)),
    cardObservationFor: (event: CardEvent, id: string) => {
      const reference = scoped(event, id);
      return observations.get(scoped(event, evidenceRefs.get(reference) ?? id));
    },
  };
}

export type ObservationLookup = ReturnType<typeof projectConversation>["observationFor"];
