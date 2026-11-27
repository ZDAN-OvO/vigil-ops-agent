export interface Access {
  approval_enabled: boolean;
  approval_disabled_reason: string | null;
}

export type SessionState = "idle" | "working" | "awaiting_approval" | "interrupted" | "closed";

export interface Session {
  id: string;
  title: string;
  title_origin?: "pending" | "auto" | "user" | "alert";
  source: "user" | "alert";
  state: SessionState;
  last_seq: number;
  pending_approval: boolean;
}

export type SessionsResponse = Access & { sessions: Session[] };
export type SessionResponse = Access & { session: Session };

interface EventEnvelope {
  session_id: string;
  seq: number;
  ts: string;
  turn_id?: string | null;
}

type EventOf<T extends string, Fields = Record<never, never>> = EventEnvelope & { type: T } & Fields;

export interface VerificationPlan {
  method?: string;
  window_seconds?: number | null;
  duration_seconds?: number;
  sample_seconds?: number;
}

export type Verification = VerificationPlan | string | null;

export type CardEvent = EventOf<"approval_required", {
  card_id: string;
  fingerprint?: string;
  status?: string;
  action: string;
  target?: { cluster?: string; namespace?: string; kind?: string; name?: string; uid?: string };
  parameters?: {
    replicas?: number; node_name?: string; restart_count?: number; last_exit_reason?: string;
    container?: string; request_cpu?: string; limit_cpu?: string;
    request_memory?: string; limit_memory?: string;
    source?: "env" | "config_map"; key?: string; value?: string; config_map?: string;
  };
  before?: Record<string, unknown> | null;
  automatic_compensation?: boolean;
  compensates?: string | null;
  risk?: { disposition: "approval" | "deny" | "auto"; reasons?: string[] };
  reason?: string;
  evidence_ids?: string[];
  verification?: Verification | { verification_plan?: Verification };
  template_diff?: { path: string; op: string; current?: unknown; value?: unknown }[];
}>;

export type Decision = "approved" | "rejected";
export type CardUpdate = EventOf<"card_decision", {
  card_id: string;
  status: string;
  expires_at?: string | null;
}> | EventOf<"action_result", {
  card_id: string;
  status: string;
  proposal_status: string;
  attempt_id?: string | null;
  reason?: string | null;
  compensated?: boolean;
  verification?: Record<string, unknown> | null;
}>;

export type RemediationProgress = EventOf<"remediation_progress", {
  card_id: string; slots_collected: number; slots_required?: number; failed_slots?: number;
}>;

export type ObservationEvent = EventOf<"tool_call", {
  id?: string | null;
  lane?: "main" | "side";
  delegated?: boolean;
  status?: "running" | "ok" | "empty" | "error" | "rejected";
  tool?: string;
  summary?: string;
  collected_at?: string | null;
  observation_id?: string | null;
  observation?: unknown;
  time_range?: { start: string; end: string } | null;
  query_scope?: {
    cluster?: string; namespace?: string; resource_name?: string; service?: string;
    runbook_id?: string; lookback_seconds?: number; step_seconds?: number; criteria_withheld?: boolean;
  };
  truncated?: boolean;
}>;

export type DeliveryEvent = EventOf<"input_delivered", { input_id: string; step?: number; lane: string }>;

export type SessionEvent =
  | CardEvent | CardUpdate | ObservationEvent | DeliveryEvent | RemediationProgress
  | EventOf<"session", { title?: string; source?: Session["source"]; resumed?: boolean; renamed?: boolean }>
  | EventOf<"session_closed">
  | EventOf<"message", {
      source?: string; role: string; text: string; input_id?: string; interim?: boolean; lane?: string;
    }>
  | EventOf<"skill", {
      name: string; description?: string; content_hash?: string;
      trigger: "signal" | "description"; status: "loaded" | "failed";
      lane: string; delegated?: boolean;
      snapshot?: { text: string; start_line: number; end_line: number; truncated: boolean };
    }>
  | EventOf<"hypothesis", { id: string; statement: string; status: string; update_reason?: string }>
  | EventOf<"evidence", {
      evidence_id?: string; id?: string; observation_id?: string;
      hypothesis_id?: string; relation?: "supporting" | "refuting" | "neutral"; rationale?: string;
    }>
  | EventOf<"result", { status: string; summary?: string }>;

export interface EventPage {
  next_after_seq: number;
  has_more: boolean;
  events: SessionEvent[];
}

export const sessionPath = (id: string) => `/sessions/${encodeURIComponent(id)}`;

class RequestError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "服务请求失败";
}

export const isMissing = (error: unknown) => error instanceof RequestError && error.status === 404;

export async function request<T>(path: string, options: {
  method?: "POST";
  body?: object;
  signal?: AbortSignal;
} = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    method: options.method,
    signal: options.signal,
    ...(options.body !== undefined && {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options.body),
    }),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new RequestError("服务响应格式无效", response.status);
  }
  if (!response.ok) throw new RequestError(data.message || "服务请求失败", response.status);
  return data as T;
}
