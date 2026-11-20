import { button, display, duration, element, spinner, stamp, time } from "./ui.js";

const states = {
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

function viewState(proposalStatus, attemptStatus) {
  // 未发送的 intent 也可能随提案过期或作废而结束，状态词以结束原因优先。
  if (["voided", "expired", "rejected", "denied"].includes(proposalStatus)) return proposalStatus;
  if (proposalStatus === "failed" || attemptStatus === "failed") return "failed";
  if (attemptStatus === "unknown") return "unknown";
  if (proposalStatus === "succeeded" && attemptStatus === "verified") return "verified";
  if (attemptStatus === "intent" || attemptStatus === "executing") return attemptStatus;
  if (!attemptStatus && ["pending_approval", "approved"].includes(proposalStatus)) {
    return proposalStatus;
  }
  return "";
}

function addField(fields, label, value, mono = false) {
  fields.append(element("dt", "stamp", label), element("dd", mono ? "mono" : "", display(value)));
}

function rollback(changes) {
  const block = element("div", "card-change");
  if (!changes.length) {
    block.textContent = "未取得模板变更明细";
    return block;
  }
  const table = element("table", "change-table");
  const cols = element("colgroup");
  cols.append(element("col"), element("col"), element("col"));
  const header = table.createTHead().insertRow();
  for (const label of ["字段", "当前", "回滚后"]) {
    const cell = element("th", "stamp", label);
    cell.scope = "col";
    header.append(cell);
  }
  table.prepend(cols);
  const rows = table.createTBody();
  for (const change of changes) {
    const row = rows.insertRow();
    const removed = change.op === "remove";
    row.insertCell().append(element("span", "mono", display(change.path)));
    row.insertCell().append(element("span", removed ? "mono warn" : "mono muted", display(change.current)));
    row.insertCell().append(
      element("span", "mono gold", "→ "), element("span", removed ? "muted" : "", removed ? "将被移除" : display(change.value)),
    );
  }
  block.append(table);
  return block;
}

function podDetails(target, parameters) {
  const block = element("dl", "fields card-change");
  addField(block, "Pod", target.name, true);
  addField(block, "UID", target.uid, true);
  addField(block, "节点", parameters.node_name, true);
  addField(block, "重启次数", parameters.restart_count, true);
  addField(block, "上次退出", parameters.last_exit_reason);
  addField(block, "预期影响", "该 Pod 会被删除，副本数由 Deployment 维持");
  return block;
}

const verificationMethods = {
  prometheus: "Prometheus 探针",
  pod_http_and_service_endpoints: "Pod HTTP 与 Service 端点",
};

function verificationPlan(plan) {
  if (!plan?.method) return display(plan);
  const method = verificationMethods[plan.method] ?? plan.method;
  const window = plan.window_seconds == null ? "" : ` · 验证窗口 ${plan.window_seconds} 秒`;
  return method + window;
}

function verificationText(verification, plan) {
  if (verification == null) return "验证结论未提供";
  const conclusion = verification.summary ?? verification.outcome_reason ?? verification.note;
  if (!conclusion) return display(verification);
  const method = verification.method ?? (verification.actor ? "人工核实" : plan?.method);
  return [verificationMethods[method] ?? method, conclusion].filter(Boolean).join(" · ");
}

export function createCard(card, { decide, observationFor }) {
  const target = card.target ?? {};
  const parameters = card.parameters ?? {};
  let proposalStatus = card.status;
  let attemptStatus;
  let expiresAt;
  let receipt;
  let access = {};
  let submitting = false;
  let submitted = false;

  const node = element("article", "card");
  node.id = `card-${card.card_id}`;
  node.tabIndex = -1;
  const head = element("header", "card-head");
  const statusLabel = stamp("", "card-state");
  statusLabel.setAttribute("role", "status");
  head.append(stamp(card.card_id), statusLabel);
  const [risk, riskClass] = riskLabels[card.risk?.disposition] ?? ["风险决策未提供", "dim"];
  const reasons = card.risk?.reasons?.join(" · ");
  const riskLabel = stamp(reasons ? `${risk} · ${reasons}` : risk, `card-risk ${riskClass}`);
  head.append(riskLabel);

  const body = element("div", "card-body");
  const titles = { rollout_undo: "回滚 ", restart_pod: "重建 Pod ", scale_replicas: "调整副本数 " };
  const title = element("h3", "card-title", titles[card.action] ?? "动作提案 ");
  title.append(element("span", "mono", display(target.name)));
  const fields = element("dl", "fields");
  const plan = card.verification?.verification_plan ?? card.verification;
  const replicaText = parameters.replicas == null ? "" : ` · 当前 ${parameters.replicas} 副本`;
  addField(fields, "目标", `${[target.cluster, target.namespace, target.kind, target.name].filter(Boolean).join(" / ")}${replicaText}`, true);
  addField(fields, "依据", card.reason);
  addField(fields, "验证方式", verificationPlan(plan));
  body.append(title, fields);
  if (card.action === "rollout_undo") body.append(rollback(card.template_diff ?? []));
  if (card.action === "restart_pod") body.append(podDetails(target, parameters));

  const evidence = element("div", "card-evidence");
  const refs = (card.evidence_ids ?? []).map((id, index) => {
    const row = element("div", "evidence-ref");
    const link = button("mono", `E${index + 1}`, () => observationFor(id)?.reveal());
    link.title = id;
    const unavailable = stamp("观测不可回查");
    row.append(link, unavailable);
    evidence.append(row);
    return { id, link, unavailable };
  });
  if (refs.length) body.append(evidence);

  const actions = element("div", "card-action-bar");
  const feedback = element("p", "notice");
  feedback.setAttribute("role", "status");
  const approved = button("approve", "批准", () => post("approved"));
  const rejected = button("reject", "拒绝", () => post("rejected"));
  actions.append(approved, rejected);
  body.append(actions, feedback);

  const foot = element("footer", "card-foot");
  const bottom = element("div", "card-foot-line");
  const countdown = element("p", "stamp gold");
  countdown.hidden = true;
  foot.append(bottom, countdown);
  node.append(head, body, foot);

  async function post(decision) {
    if (approved.disabled || !card.fingerprint) return;
    submitting = true;
    feedback.textContent = "正在提交决定…";
    renderActions();
    try {
      await decide(card, decision);
      submitted = true;
      if (node.dataset.state === "pending_approval") feedback.textContent = "已提交 · 等待账本更新";
    } catch (error) {
      if (node.dataset.state === "pending_approval") feedback.textContent = error.message;
    } finally {
      submitting = false;
      renderActions();
    }
  }

  function renderActions() {
    const pending = node.dataset.state === "pending_approval";
    actions.hidden = !pending;
    if (!pending) {
      feedback.textContent = "";
    } else if (!submitting && !submitted && !access.approval_enabled) {
      feedback.textContent = `只读 · ${access.approval_disabled_reason || "当前无法审批"}`;
    } else if (!card.fingerprint) {
      feedback.textContent = "只读 · 未取得提案指纹";
    } else if (feedback.textContent.startsWith("只读")) {
      feedback.textContent = "";
    }
    approved.disabled = rejected.disabled = !access.approval_enabled || !card.fingerprint
      || submitting || submitted;
  }

  function tick(now) {
    if (countdown.hidden) return;
    const remaining = Math.max(0, Math.ceil((Date.parse(expiresAt) - now) / 1000));
    countdown.textContent = `授权到期 ${time(expiresAt, true)} · 剩 ${duration(remaining)}`;
  }

  function renderState(animate = false) {
    const state = viewState(proposalStatus, attemptStatus);
    const [label, color, note] = states[state] ?? ["状态未取到", "dim", "等待服务端回执"];
    const lostFocus = state !== "pending_approval" && actions.contains(document.activeElement);
    node.dataset.state = state;
    statusLabel.className = `stamp card-state ${color}`;
    statusLabel.textContent = label;
    if (state === "pending_approval") statusLabel.prepend(element("span", "flag"));
    let footer = note;
    if (state === "failed") footer = `${receipt?.reason || "失败原因未提供"} · 目标状态见第 9 节`;
    if (state === "verified") footer = `结果 · ${verificationText(receipt?.verification, plan)}`;
    bottom.className = `card-foot-line ${color}`;
    bottom.textContent = footer;
    if (state === "executing") bottom.append(spinner());
    countdown.hidden = !["approved", "intent", "executing"].includes(state)
      || !Number.isFinite(Date.parse(expiresAt));
    if (countdown.hidden) {
      countdown.textContent = "";
      countdown.removeAttribute("title");
    } else {
      countdown.title = expiresAt;
    }
    tick(Date.now());
    renderActions();
    if (animate) {
      for (const part of [statusLabel, foot]) {
        part.classList.remove("card-status-change");
        void part.offsetWidth;
        part.classList.add("card-status-change");
      }
    }
    if (lostFocus) node.focus({ preventScroll: true });
  }

  function apply(event) {
    if (event.type === "card_decision") {
      proposalStatus = event.status;
      expiresAt = event.status === "approved" ? event.expires_at : undefined;
    } else {
      proposalStatus = event.proposal_status;
      attemptStatus = event.attempt_id ? event.status : undefined;
      receipt = event;
    }
    renderState(true);
  }

  function setAccess(value) {
    access = value;
    renderActions();
  }

  function updateEvidence() {
    for (const { id, link, unavailable } of refs) {
      const available = Boolean(observationFor(id));
      link.disabled = !available;
      unavailable.hidden = available;
    }
  }

  renderState();
  return { node, apply, setAccess, tick, updateEvidence };
}
