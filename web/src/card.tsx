import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Access, CardEvent, CardUpdate, Decision, Verification } from "./api";
import { errorMessage } from "./api";
import type { ObservationJump } from "./investigation-details";
import { display, duration, Spinner, Stamp, time } from "./ui";

const states: Record<string, [label: string, color: string, note?: string]> = {
  pending_approval: ["待审批", "dim", "等待人工批准 · 授权在批准时才生成"],
  approved: ["已批准 · 待执行", "gold", "已批准 · 等待租约持有者执行"],
  intent: ["准备写入", "gold", "已取得动作锁 · 尚未取得发送权"],
  executing: ["执行中", "gold", "已取得发送权 · 结果待定"],
  verified: ["已验证", "ok"],
  failed: ["失败", "danger"],
  unknown: ["不确定", "danger", "结果不确定 · 目标保持锁定，等待人工核实"],
  rejected: ["已拒绝", "muted", "未写入集群"],
  denied: ["策略拒绝", "muted", "未写入集群"],
  expired: ["已过期", "danger", "未写入集群"],
  voided: ["已作废", "muted", "未写入集群"],
};

const riskLabels = {
  approval: ["需人工批准", "warn"],
  deny: ["策略拒绝", "danger"],
  auto: ["规则允许", "ok"],
};

function viewState(proposal?: string, attempt?: string): string {
  // 未发送的 intent 也可能随提案过期或作废，以结束原因为准。
  if (proposal && ["voided", "expired", "rejected", "denied"].includes(proposal)) return proposal;
  if (proposal === "failed" || attempt === "failed") return "failed";
  if (attempt === "unknown") return "unknown";
  if (proposal === "succeeded" && attempt === "verified") return "verified";
  if (attempt === "intent" || attempt === "executing") return attempt;
  if (!attempt && (proposal === "pending_approval" || proposal === "approved")) return proposal;
  return "";
}

const verificationMethods: Record<string, string> = {
  prometheus: "Prometheus 探针",
  pod_http_and_service_endpoints: "Pod HTTP 与 Service 端点",
};

function verificationPlan(plan?: Verification) {
  if (typeof plan !== "object" || !plan?.method) return display(plan);
  const method = verificationMethods[plan.method] ?? plan.method;
  return method + (plan.window_seconds == null ? "" : ` · 验证窗口 ${plan.window_seconds} 秒`);
}

function verificationText(result?: Record<string, unknown> | null, plan?: Verification) {
  if (result == null) return "验证结论未提供";
  const conclusion = result.summary ?? result.outcome_reason ?? result.note;
  if (!conclusion) return display(result);
  const method = result.method ?? (result.actor ? "人工核实" : typeof plan === "object" && plan?.method);
  return [typeof method === "string" && (verificationMethods[method] ?? method), conclusion]
    .filter(Boolean).join(" · ");
}

function Field({ label, value, mono }: { label: string; value: unknown; mono?: boolean }) {
  return <><dt className="stamp">{label}</dt><dd className={mono ? "mono" : undefined}>{display(value)}</dd></>;
}

function Rollback({ changes = [] }: { changes?: CardEvent["template_diff"] }) {
  return <div className="card-change">
    {!changes.length ? "未取得模板变更明细" : <table className="change-table">
      <colgroup><col /><col /><col /></colgroup>
      <thead><tr>
        {["字段", "当前", "回滚后"].map((label) => <th className="stamp" scope="col" key={label}>{label}</th>)}
      </tr></thead>
      <tbody>
        {changes.map((change) => {
          const removed = change.op === "remove";
          return <tr key={change.path}>
            <td className="mono">{display(change.path)}</td>
            <td className={removed ? "mono warn" : "mono muted"}>{display(change.current)}</td>
            <td><span className="mono gold">→ </span>
              <span className={removed ? "muted" : undefined}>
                {removed ? "将被移除" : display(change.value)}
              </span>
            </td>
          </tr>;
        })}
      </tbody>
    </table>}
  </div>;
}

function Countdown({ expiresAt }: { expiresAt: string }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const remaining = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000));
  return <p className="stamp gold" title={expiresAt}>
    授权到期 {time(expiresAt, true)} · 剩 {duration(remaining)}
  </p>;
}

interface CardProps {
  card: CardEvent;
  update?: CardUpdate;
  expiresAt?: string | null;
  access: Access;
  anchored: boolean;
  decide: (card: CardEvent, decision: Decision) => Promise<void>;
  observationFor: (id: string) => string | undefined;
  reveal: (jump: ObservationJump) => void;
}

const titles: Record<string, string> = { rollout_undo: "回滚 ", restart_pod: "重建 Pod ", scale_replicas: "调整副本数 " };

export function Card({ card, update, expiresAt, access, anchored, decide, observationFor, reveal }: CardProps) {
  const { target = {}, parameters = {} } = card;
  const plan = card.verification && typeof card.verification === "object" && "verification_plan" in card.verification
    ? card.verification.verification_plan : card.verification as Verification | undefined;
  const receipt = update?.type === "action_result" ? update : undefined;
  const proposal = receipt ? receipt.proposal_status : update?.status ?? card.status;
  const state = viewState(proposal, receipt?.attempt_id ? receipt.status : undefined);
  const [label, color, note] = states[state] ?? ["状态未取到", "dim", "等待服务端回执"];
  const pending = state === "pending_approval";
  const [risk, riskClass] = (card.risk && riskLabels[card.risk.disposition]) ?? ["风险决策未提供", "dim"];
  const targetText = [target.cluster, target.namespace, target.kind, target.name].filter(Boolean).join(" / ");
  const replicas = parameters.replicas == null ? "" : ` · 当前 ${parameters.replicas} 副本`;
  const footer = state === "failed" ? `${receipt?.reason || "失败原因未提供"} · 目标状态见第 9 节`
    : state === "verified" ? `结果 · ${verificationText(receipt?.verification, plan)}` : note;
  const countdown = expiresAt && ["approved", "intent", "executing"].includes(state)
    && Number.isFinite(Date.parse(expiresAt));
  const animation = update ? " card-status-change" : "";

  const [submission, setSubmission] = useState<"waiting" | "posted" | null>(null);
  const [failure, setFailure] = useState("");
  const busy = useRef(false);
  const node = useRef<HTMLElement>(null);
  const actionsFocused = useRef(false);
  const allowed = access.approval_enabled && Boolean(card.fingerprint);

  useLayoutEffect(() => {
    if (anchored) {
      node.current?.scrollIntoView({ block: "start" });
      node.current?.focus({ preventScroll: true });
    }
  }, [anchored]);

  useLayoutEffect(() => {
    if (!pending && actionsFocused.current) node.current?.focus({ preventScroll: true });
  }, [pending]);

  async function submit(decision: Decision) {
    if (!pending || !allowed || busy.current) return;
    busy.current = true;
    setFailure("");
    setSubmission("waiting");
    try {
      await decide(card, decision);
      setSubmission("posted");
    } catch (error) {
      busy.current = false;
      setSubmission(null);
      setFailure(errorMessage(error));
    }
  }

  const feedback = submission === "waiting" ? "正在提交决定…"
    : submission === "posted" ? "已提交 · 等待账本更新"
    : !access.approval_enabled ? `只读 · ${access.approval_disabled_reason || "当前无法审批"}`
    : !card.fingerprint ? "只读 · 未取得提案指纹" : failure;

  return <article id={`card-${card.card_id}`} tabIndex={-1} ref={node}
    className={anchored ? "card anchor-target" : "card"} data-state={state}>
    <header className="card-head">
      <Stamp>{card.card_id}</Stamp>
      <span className={`stamp card-state ${color}${animation}`} role="status" key={update?.seq}>
        {pending && <span className="flag" />}{label}
      </span>
      <Stamp className={`card-risk ${riskClass}`}>{[risk, ...(card.risk?.reasons ?? [])].join(" · ")}</Stamp>
    </header>
    <div className="card-body">
      <h3 className="card-title">{titles[card.action] ?? "动作提案 "}<span className="mono">{display(target.name)}</span></h3>
      <dl className="fields">
        <Field label="目标" value={targetText + replicas} mono />
        <Field label="依据" value={card.reason} />
        <Field label="验证方式" value={verificationPlan(plan)} />
      </dl>
      {card.action === "rollout_undo" && <Rollback changes={card.template_diff} />}
      {card.action === "restart_pod" && <dl className="fields card-change">
        <Field label="Pod" value={target.name} mono />
        <Field label="UID" value={target.uid} mono />
        <Field label="节点" value={parameters.node_name} mono />
        <Field label="重启次数" value={parameters.restart_count} mono />
        <Field label="上次退出" value={parameters.last_exit_reason} />
        <Field label="预期影响" value="该 Pod 会被删除，副本数由 Deployment 维持" />
      </dl>}
      {!!card.evidence_ids?.length && <div className="card-evidence">
        {card.evidence_ids.map((id, index) => {
          const observation = observationFor(id);
          return <div className="evidence-ref" key={id}>
            <button type="button" className="mono" title={id} disabled={observation === undefined}
              onClick={(event) => {
                if (observation !== undefined) reveal({ key: observation, origin: event.currentTarget });
              }}>E{index + 1}</button>
            {observation === undefined && <Stamp>观测不可回查</Stamp>}
          </div>;
        })}
      </div>}
      <div className="card-action-bar" hidden={!pending}
        onFocus={() => { actionsFocused.current = true; }}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) actionsFocused.current = false;
        }}>
        <button type="button" className="approve" disabled={!allowed} aria-disabled={!!submission || undefined}
          onClick={() => void submit("approved")}>批准</button>
        <button type="button" className="reject" disabled={!allowed} aria-disabled={!!submission || undefined}
          onClick={() => void submit("rejected")}>拒绝</button>
      </div>
      <p className="notice" role="status">{pending ? feedback : ""}</p>
    </div>
    <footer className={`card-foot${animation}`} key={update?.seq}>
      <div className={`card-foot-line ${color}`}>{footer}{state === "executing" && <Spinner />}</div>
      {countdown && <Countdown expiresAt={expiresAt} />}
    </footer>
  </article>;
}
