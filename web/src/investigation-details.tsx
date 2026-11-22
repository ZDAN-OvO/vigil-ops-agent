import { Collapsible } from "@base-ui/react/collapsible";
import { type ReactNode, useCallback, useLayoutEffect, useRef, useState } from "react";
import type { ObservationEvent } from "./api";
import type {
  EvidenceEvent, Hypothesis, InvestigationProcess, ObservationLookup, SkillEvent,
} from "./conversation-projection";
import { Stamp, time } from "./ui";

const tools: Record<string, string> = {
  "prometheus.query_range": "查询时序指标",
  "kubernetes.list_deployments": "查询 Deployment",
  "kubernetes.list_pods": "查询 Pod",
  "kubernetes.list_events": "查询 Kubernetes 事件",
  "kubernetes.list_services": "查询 Service",
  "kubernetes.list_logs": "查询 Pod 日志",
  "knowledge.list_runbooks": "查找 Runbook",
  "knowledge.get_runbook": "读取 Runbook",
};

const toolStates = {
  running: ["正在查询", "gold"], ok: ["已完成", "ok"], empty: ["无数据", "dim"],
  error: ["查询失败", "danger"], rejected: ["未执行", "warn"], incomplete: ["查询未完成", "warn"],
};

const laneLabel = (lane?: string) => lane === "main" ? "主线" : lane === "side" ? "旁路" : "来源未记录";

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return <><dt className="stamp">{label}</dt><dd>{children}</dd></>;
}

function describeObservation(event: ObservationEvent, finished?: boolean) {
  const status = event.status === "running" && finished ? "incomplete" : event.status;
  const [state, color] = status ? toolStates[status] ?? ["状态未记录", "dim"] : ["状态未记录", "dim"];
  return {
    status, state, color, tool: tools[event.tool ?? ""] ?? "只读查询",
    summary: status !== "incomplete" && event.summary !== state ? event.summary : undefined,
  };
}

export interface ObservationJump {
  key: string;
  origin: HTMLButtonElement;
  evidence?: { statement?: string; relation?: EvidenceEvent["relation"]; rationale: string };
}

type RevealObservation = (jump: ObservationJump) => void;

interface JumpProps {
  jump?: ObservationJump;
  dismissJump: () => void;
}

function Observation({ event, jump, dismissJump, finished, openProcess }: JumpProps & {
  event: ObservationEvent; finished?: boolean; openProcess: () => void;
}) {
  const [open, setOpen] = useState(false);
  const node = useRef<HTMLElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  useLayoutEffect(() => {
    if (!jump) return;
    openProcess();
    setOpen(true);
  }, [jump, openProcess]);

  useLayoutEffect(() => {
    if (!jump || !open) return;
    node.current?.scrollIntoView?.({ block: "start" });
    trigger.current?.focus({ preventScroll: true });
  }, [jump, open]);

  function returnToReference() {
    dismissJump();
    jump?.origin.scrollIntoView?.({ block: "nearest" });
    jump?.origin.focus({ preventScroll: true });
  }

  const { tool, status, state, color, summary } = describeObservation(event, finished);
  const details = event.observation;
  const scope = event.query_scope;
  const range = event.time_range;
  const scopeParts = [scope?.cluster, scope?.namespace, scope?.resource_name, scope?.service, scope?.runbook_id]
    .filter(Boolean);
  return <Collapsible.Root render={<section ref={node} />} open={open} onOpenChange={(next) => {
    setOpen(next);
    if (!next && jump) dismissJump();
  }} className="observation" data-status={status}>
    <header className="observation-head">
      <div>
        <span>{tool}</span>{" · "}<Stamp className={color}>{state}</Stamp>
        <p className="stamp">{laneLabel(event.lane)}{event.delegated && " · 只读委派"}
          {event.collected_at && <>{" · "}<time dateTime={event.collected_at}
            title={event.collected_at}>{time(event.collected_at, true)}</time></>}</p>
      </div>
      <Collapsible.Trigger ref={trigger} className="disclosure" aria-label="展开或收起观测">
        {open ? "[−]" : "[+]"}
      </Collapsible.Trigger>
    </header>
    {summary && <p className="observation-summary">{summary}</p>}
    <Collapsible.Panel keepMounted className={open ? "fold-body open" : "fold-body"} inert={!open}>
      <div>
        {jump && <div className="observation-reference">
          {jump.evidence && <div>
            {jump.evidence.statement && <p className="observation-reference-title">{jump.evidence.statement}</p>}
            <p><RelationStamp relation={jump.evidence.relation} /> {jump.evidence.rationale}</p>
          </div>}
          <button type="button" className="source-link" onClick={returnToReference}>
            {jump.evidence ? "返回依据" : "返回卡片引用"}
          </button>
        </div>}
        <dl className="investigation-fields">
          <Detail label="工具">{event.tool || "未记录"}</Detail>
          <Detail label="查询对象">{scopeParts.length ? scopeParts.join(" / ") : "未记录"}</Detail>
          {scope?.criteria_withheld && <Detail label="查询条件">未公开</Detail>}
          {(scope?.lookback_seconds != null || scope?.step_seconds != null) &&
            <Detail label="查询参数">{[
              scope.lookback_seconds != null && `回看 ${scope.lookback_seconds} 秒`,
              scope.step_seconds != null && `步长 ${scope.step_seconds} 秒`,
            ].filter(Boolean).join(" · ")}</Detail>}
          <Detail label="采集时间">{event.collected_at
            ? <time dateTime={event.collected_at} title={event.collected_at}>{time(event.collected_at, true)}</time>
            : "未记录"}</Detail>
          <Detail label="时间范围">{range
            ? <><time dateTime={range.start} title={range.start}>{time(range.start, true)}</time>{" — "}
              <time dateTime={range.end} title={range.end}>{time(range.end, true)}</time></> : "未记录"}</Detail>
          {event.truncated && <Detail label="结果">已截断</Detail>}
        </dl>
        <pre tabIndex={open ? 0 : undefined}>{details == null
          ? (status === "running" ? "等待结果" : "观测详情未记录")
          : typeof details === "string" ? details : JSON.stringify(details, null, 2)}</pre>
      </div>
    </Collapsible.Panel>
  </Collapsible.Root>;
}

function SkillDetails({ event }: { event: SkillEvent }) {
  const { snapshot } = event;
  return <details className="skill-details">
    <summary><span>Skill · {event.name}</span>{" · "}
      <Stamp className={event.status === "loaded" ? "ok" : "danger"}>
        {event.status === "loaded" ? "已加载" : "读取失败"}
      </Stamp>
    </summary>
    <dl className="investigation-fields">
      <Detail label="来源">{event.trigger === "signal" ? "信号触发" : "主动读取"}
        {" · "}{laneLabel(event.lane)}{event.delegated && " · 只读委派"}</Detail>
      <Detail label="版本">{event.content_hash ? <span className="mono">{event.content_hash}</span> : "未记录"}</Detail>
      {snapshot && <Detail label="读取位置">第 {snapshot.start_line}–{snapshot.end_line} 行
        {snapshot.truncated && " · 正文已截断"}</Detail>}
    </dl>
    {event.description && <p>{event.description}</p>}
    {snapshot ? <pre tabIndex={0}>{snapshot.text}</pre> : <p className="muted">正文未记录</p>}
  </details>;
}

export function ProcessDetails({ process, jump, dismissJump }: JumpProps & {
  process: InvestigationProcess;
}) {
  const details = useRef<HTMLDetailsElement>(null);
  const openProcess = useCallback(() => {
    if (details.current) details.current.open = true;
  }, []);
  const counts = { tool_call: 0, skill: 0 };
  const notices: ReactNode[] = [];
  let running = 0;
  for (const { key, event, finished } of process.items) {
    counts[event.type] += 1;
    if (event.type === "skill") {
      if (event.status === "failed") {
        notices.push(<li key={key}><Stamp className="danger">Skill 读取失败</Stamp> {event.name}</li>);
      }
      continue;
    }
    const { status, state, color, tool, summary } = describeObservation(event, finished);
    if (status === "running") running += 1;
    if (status === "error" || status === "empty" || status === "rejected" || status === "incomplete") {
      notices.push(<li key={key}><Stamp className={color}>{state}</Stamp> {tool}
        {summary && ` · ${summary}`}</li>);
    }
  }
  const summary = [
    process.lane && laneLabel(process.lane),
    counts.tool_call > 0 && `${counts.tool_call} 次查询`,
    counts.skill > 0 && `${counts.skill} 项 Skill`,
    running > 0 && `${running} 次正在查询`,
  ].filter(Boolean).join(" · ");

  return <section className="investigation-process">
    <details className="process-fold" ref={details} onToggle={(event) => {
      if (jump && event.target === event.currentTarget && !event.currentTarget.open) dismissJump();
    }}>
      <summary>调查过程 <Stamp>{summary}</Stamp></summary>
      <div className="process-items">
        {process.items.map(({ key, event, finished }) => event.type === "tool_call"
          ? <Observation key={key} event={event} finished={finished}
            openProcess={openProcess} jump={jump?.key === key ? jump : undefined} dismissJump={dismissJump} />
          : <SkillDetails key={key} event={event} />)}
      </div>
    </details>
    {notices.length > 0 && <ul className="process-notices">{notices}</ul>}
  </section>;
}

const hypothesisStates: Record<string, [string, string]> = {
  pending: ["待核实", "dim"], supported: ["有依据支持", "ok"],
  refuted: ["已反驳", "danger"], inconclusive: ["证据不足", "warn"],
};

const relations = {
  supporting: ["支持", "ok"], refuting: ["反驳", "danger"], neutral: ["中性", "dim"],
};

function RelationStamp({ relation }: { relation?: EvidenceEvent["relation"] }) {
  const [label, color] = relation ? relations[relation] ?? ["依据", "dim"] : ["依据", "dim"];
  return <Stamp className={color}>{label}</Stamp>;
}

interface EvidenceProps {
  observationFor: ObservationLookup;
  reveal: RevealObservation;
}

export function Evidence({ event, statement, observationFor, reveal }: EvidenceProps & {
  event: EvidenceEvent; statement?: string;
}) {
  const rationale = event.rationale || "理由未记录";
  const reference = event.observation_id ? observationFor(event, event.observation_id) : undefined;
  return <div className="hypothesis-evidence">
    <RelationStamp relation={event.relation} />
    <span>{rationale}</span>
    {reference === undefined ? <Stamp>观测不可回查</Stamp>
      : <button type="button" className="source-link" onClick={(click) => reveal({
        key: reference, origin: click.currentTarget, evidence: { statement, relation: event.relation, rationale },
      })}>查看观测</button>}
  </div>;
}

export function HypothesisDetails({ hypothesis, observationFor, reveal }: EvidenceProps & { hypothesis: Hypothesis }) {
  const { latest, evidence, history } = hypothesis;
  const [state, color] = hypothesisStates[latest.status] ?? ["状态未记录", "dim"];
  const links = (items: Iterable<EvidenceEvent>, statement: string) => Array.from(items, (event) =>
    <Evidence key={event.observation_id ?? event.evidence_id ?? event.seq} event={event}
      statement={statement} observationFor={observationFor} reveal={reveal} />);
  return <section className="hypothesis" data-hypothesis={latest.id} data-status={latest.status}>
    <header className="hypothesis-head">
      <Stamp>假设</Stamp><Stamp className={`hypothesis-state ${color}`}><span>{state}</span></Stamp>
      <h2>{latest.statement}</h2>
    </header>
    <div className="hypothesis-basis">
      {latest.update_reason && <p className="ink-2">{latest.update_reason}</p>}
      {evidence.size ? links(evidence.values(), latest.statement) : <p className="muted">暂无关联依据</p>}
    </div>
    <details className="hypothesis-history">
      <summary>判断历史 <span className="mono">{history.length}</span></summary>
      <ol>
        {history.map(({ change, evidence: previous }) => {
          const [label, tone] = hypothesisStates[change.status] ?? ["状态未记录", "dim"];
          return <li key={change.seq}>
            <div className="hypothesis-version">
              <Stamp>#{change.seq}</Stamp><time dateTime={change.ts}>{time(change.ts, true)}</time>
              <Stamp className={tone}>{label}</Stamp>
            </div>
            <p>{change.statement}</p>
            <p className="muted">{change.update_reason || "变化理由未记录"}</p>
            {links(previous, change.statement)}
          </li>;
        })}
      </ol>
    </details>
  </section>;
}
