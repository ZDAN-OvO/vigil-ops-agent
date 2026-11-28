import type { InspectionAction, InspectionSummaryEvent } from "./api";
import { Stamp } from "./ui";

const runStates = {
  running: "巡检中", completed: "已完成", timed_out: "整轮超时", interrupted: "巡检中断", failed: "巡检失败",
};
const checkNames: Record<string, string> = {
  "workload-readiness": "工作负载就绪", "workload-signal": "服务业务指标",
  "node-ready": "节点就绪", "entry-probe": "入口探测",
};
const observationStates = { healthy: "健康", abnormal: "异常", insufficient: "信息不足" };
const observationStyles = { healthy: "ok", abnormal: "danger", insufficient: "warn" };
const reasons: Record<string, string> = {
  timeout: "采集超时", deadline_exceeded: "达到本轮截止时间", stopped: "服务停止",
  lease_expired: "巡检执行者失去租约", execution_failed: "执行巡检时出错", not_collected: "本轮未采集",
  collection_failed: "采集失败", stale_sample: "样本已过期", stale_generation: "工作负载状态未更新",
  missing_data: "缺少数据", invalid_sample: "样本无效", ambiguous_data: "存在多个匹配结果",
  truncated: "采集结果截断", probe_scrape_unavailable: "探针抓取不可用",
  threshold_not_met: "未达到登记阈值", write_identity_or_policy_unavailable: "缺少可用的写入身份或策略",
};
const statuses: Record<string, string> = {
  send_granted: "已取得发送权", applied: "已写入，等待业务验证", succeeded: "验证完成",
  failed: "处置失败", voided: "发送前作废", unknown: "结果待核实", compensation_ready: "等待补偿",
  improved: "业务恢复", no_improvement: "业务尚未恢复", worsened: "业务恶化",
  reviewed: "已核对证据", manual_review: "需人工核实", abnormal: "异常", healthy: "健康",
  cleared: "已结案", probe_unverified: "探针尚未核实", insufficient: "信息不足",
  target_unavailable: "目标不可用", selected: "规则已选中", lower_priority: "优先级较低",
  not_matched: "规则未命中", automatic_disabled: "自动处置已关闭", proposal_created: "已创建动作",
  fault_already_handled: "该故障已使用自动处置名额", review_unconfirmed: "证据未确认",
};

const reason = (code: string) => reasons[code] ?? code;
const status = (code: string) => statuses[code] ?? code;
const dateTime = (value: string) => new Date(value).toLocaleString("zh-CN", { hour12: false });

function Receipt({ id, action }: { id: string; action: InspectionAction }) {
  return <p>
    {action.parent_id && "补偿 · "}{status(action.status)}
    {action.outcome && !action.parent_id && ` · ${status(action.outcome)}`}
    {action.code && ` · ${reason(action.code)}`}
    {" · "}<code>{id}</code>
  </p>;
}

function receiptHistory(history: InspectionSummaryEvent[]) {
  const latest = new Map<string, string>();
  return history.flatMap((snapshot) => Object.entries(snapshot.result?.actions ?? {}).flatMap(([id, action]) => {
    const signature = JSON.stringify(action);
    if (latest.get(id) === signature) return [];
    latest.set(id, signature);
    return [{ id, action, seq: snapshot.seq, ts: snapshot.ts }];
  }));
}

export function InspectionSummary({ event, history }: {
  event: InspectionSummaryEvent;
  history: InspectionSummaryEvent[];
}) {
  const { result, observations } = event;
  const initial = history.find((snapshot) => snapshot.observations.length > 0) ?? history[0];
  const receipts = receiptHistory(history);
  return <section className="message inspection-summary" aria-label="巡检摘要">
    <h2>巡检摘要 <Stamp>{runStates[event.status] ?? event.status}</Stamp></h2>
    {event.reason && <p>{reason(event.reason)}</p>}
    <dl className="investigation-fields">
      <dt>集群</dt><dd>{event.cluster}</dd>
      <dt>计划时间</dt><dd><time dateTime={event.scheduled_at}>{dateTime(event.scheduled_at)}</time></dd>
      <dt>截止时间</dt><dd><time dateTime={event.deadline}>{dateTime(event.deadline)}</time></dd>
      {event.finished_at && <><dt>结束时间</dt>
        <dd><time dateTime={event.finished_at}>{dateTime(event.finished_at)}</time></dd></>}
    </dl>
    {observations.length > 0 && <div className="process-items">{observations.map((observation) => {
      const { target, check_id, status, sampled_at, evidence_refs } = observation;
      return <article className="observation" key={observation.id}>
        <div className="observation-head">
          <span>{checkNames[check_id] ?? check_id} · {target.probe_id ||
            [target.namespace, target.kind, target.name].filter(Boolean).join(" / ")}</span>
          <Stamp className={observationStyles[status]}>{observationStates[status] ?? status}</Stamp>
        </div>
        <div className="observation-summary">
          {observation.reason && <p>{reason(observation.reason)}</p>}
          {sampled_at && <p>采样 <time dateTime={sampled_at}>{dateTime(sampled_at)}</time></p>}
          {evidence_refs.map((ref) => <p key={ref}>证据 <code>{ref}</code></p>)}
        </div>
      </article>;
    })}</div>}
    {result?.investigation?.status && <section className="inspection-section">
      <h3>调查 <Stamp>{status(result.investigation.status)}</Stamp></h3>
      {result.investigation.explanation && <p>{result.investigation.explanation}</p>}
      {result.investigation.reason && <p>{reason(result.investigation.reason)}</p>}
    </section>}
    {result?.remediation_reason && <p>{reason(result.remediation_reason)}</p>}
    {result?.manual_review_required && <p>需人工核实</p>}
    {!!result?.faults?.length && <section className="inspection-section">
      <h3>故障</h3>
      {result.faults.map((fault) => <p key={fault.fault_id ?? fault.target}>
        {fault.target} · {status(fault.status)}{fault.reason && ` · ${reason(fault.reason)}`}
      </p>)}
    </section>}
    {!!result?.rules?.length && <details className="process-fold">
      <summary>处置规则</summary>
      <div className="process-items">{result.rules.map((rule) => <div
        key={JSON.stringify([rule.target, rule.rule_id, rule.status])}>
        <p>{rule.target} · {rule.rule_id} · {status(rule.status)}</p>
        {rule.review && <p>{rule.review}</p>}
        {rule.reason && <p>{reason(rule.reason)}</p>}
      </div>)}</div>
    </details>}
    {!!Object.keys(result?.actions ?? {}).length && <section className="inspection-section">
      <h3>动作回执</h3>
      {Object.entries(result?.actions ?? {}).map(([id, action]) => <Receipt id={id} action={action} key={id} />)}
    </section>}
    {initial && initial.seq !== event.seq && <details className="process-fold">
      <summary>巡检记录</summary>
      <div className="process-items">
        <p><time dateTime={initial.ts}>{dateTime(initial.ts)}</time> · 初始摘要 · {runStates[initial.status]}
          {" · "}{initial.observations.length} 项检查</p>
        {receipts.map(({ id, action, seq, ts }) => <div key={`${seq}:${id}`}>
          <time dateTime={ts}>{dateTime(ts)}</time>
          <Receipt id={id} action={action} />
        </div>)}
      </div>
    </details>}
  </section>;
}
