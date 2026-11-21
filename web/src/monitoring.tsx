import { Collapsible } from "@base-ui/react/collapsible";
import { type ReactNode, useCallback, useState } from "react";
import { Link } from "react-router";
import { request, sessionPath } from "./api";
import { usePolling } from "./polling";
import { display, duration, Stamp, time } from "./ui";

const titles = {
  alerts: "正在触发的告警",
  probes: "入口探测",
  changes: "最近变更 · 24 小时",
  services: "异常服务",
  warnings: "Warning 事件 · 1 小时",
  nodes: "节点",
};

type PanelKey = keyof typeof titles;
type Ask = (question: string) => void;
interface Block<T> {
  status: "ok" | "partial" | "missing" | "failed";
  data: T | null;
  missing?: string[];
  failed?: string[];
}
interface AlertGroup {
  group_key: string;
  name: string;
  alerts: { fingerprint: string; severity?: string; suppressed: boolean; duration_seconds?: number }[];
  session?: { session_id: string; pending_approval: boolean } | null;
}
type Samples = [number, number | null][];
interface Probe {
  address: string;
  availability: number | null;
  state: string;
  http_status?: number | null;
  duration_seconds?: number | null;
  points?: Samples | null;
}
interface Change {
  namespace?: string;
  service: string;
  version: string;
  kind: string;
  occurred_at: string;
  approved_by?: string;
}
interface Issue {
  kind: string;
  ready?: number;
  desired?: number;
  pod?: string;
  estimated?: boolean;
  restart_count_last_hour?: number;
  occurred_at?: string;
  reason?: string;
  resource?: string;
  container?: string;
  unit?: string;
  usage?: number;
  limit?: number;
  ratio?: number;
}
interface NamedService { name: string; kind: string }
interface AbnormalService extends NamedService {
  issues: Issue[];
  last_exits?: { reason: string; exit_code?: number; occurred_at?: string }[];
  missing?: string[];
}
interface Warning {
  reason: string;
  object: { kind: string; name: string };
  message: string;
  count: number;
  last_seen: string;
}
interface Snapshot {
  abnormal_service_count: number | null;
  grafana_links?: Partial<Record<PanelKey, string>>;
  alerts: Block<{ groups: AlertGroup[] }>;
  probes: Block<{ applications: { application: string; targets: Probe[] }[] }>;
  changes: Block<{ items: Change[] }>;
  services: Block<{ abnormal: AbnormalService[]; healthy: NamedService[]; unknown: NamedService[] }>;
  warnings: Block<{ items: Warning[] }>;
  nodes: Block<{
    abnormal: { name: string; conditions: { condition: string; status: string }[] }[];
    unknown: { name: string; missing_conditions: string[] }[];
    healthy: string[];
  }>;
}
type PanelData<K extends PanelKey> = NonNullable<Snapshot[K]["data"]>;

const percent = (ratio?: number | null) => ratio == null || !Number.isFinite(ratio)
  ? "—" : `${(ratio * 100).toFixed(0)}%`;

function Row({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <p className={`monitor-row ${className}`}>{children}</p>;
}

function Question({ ask, question, children }: { ask: Ask; question: string; children: ReactNode }) {
  return <button type="button" className="monitor-row monitor-question" onClick={() => ask(question)}>
    <span>{children}</span>
    <span className="stamp ask-label gold" aria-hidden="true">↵ 提问</span>
  </button>;
}

interface FoldState { open: boolean; setOpen: (value: boolean) => void }

function Fold({ label, names, open, setOpen }: FoldState & { label: string; names: string[] }) {
  return <Collapsible.Root className="monitor-row" open={open} onOpenChange={setOpen}>
    <Collapsible.Trigger className="disclosure" aria-label={label}>{open ? "[−]" : "[+]"}</Collapsible.Trigger>
    <span className="mono">{label}</span>
    <Collapsible.Panel keepMounted className={open ? "fold-body open" : "fold-body"} inert={!open}>
      <div>{names.map((name, index) => <p className="mono dim monitor-known" key={`${name}-${index}`}>{name}</p>)}</div>
    </Collapsible.Panel>
  </Collapsible.Root>;
}

function Sparkline({ points, down }: { points?: Samples | null; down: boolean }) {
  const series = points ?? [];
  const commands: string[] = [];
  const isSample = (index: number) => [0, 1].some((value) => value === series[index]?.[1]);
  const step = 59 / Math.max(1, series.length - 1);
  for (const [index, [, value]] of series.entries()) {
    if (!isSample(index)) continue;
    const x = .5 + index * step;
    const y = value ? 2 : 14;
    commands.push(`${isSample(index - 1) ? "L" : "M"}${x.toFixed(2)},${y}`);
    if (!isSample(index - 1) && !isSample(index + 1)) {
      commands.push(`L${Math.min(59.5, x + .5).toFixed(2)},${y}`);
    }
  }
  return <span className="spark-group">
    <Stamp className="stamp-small">近 1 小时</Stamp>
    {commands.length ? <svg viewBox="0 0 60 16" className={`spark ${down ? "danger" : "ok"}`} aria-hidden="true">
      <path d={commands.join(" ")} />
    </svg> : <span className="spark mono dim">—</span>}
    <Stamp className="stamp-small">可用率</Stamp>
  </span>;
}

function alertRows({ groups }: PanelData<"alerts">) {
  return groups.map((group) => <div className="monitor-subgroup" key={group.group_key || group.name}>
    {group.alerts.map((alert) => <p className="monitor-row monitor-alert" key={alert.fingerprint}>
      {!alert.suppressed && <span className="flag" />}
      <span className="mono">{group.name}</span>
      <Stamp className={alert.severity === "critical" ? "danger" : alert.severity === "warning" ? "warn" : "dim"}>
        {alert.severity || "—"}
      </Stamp>
      <span className="mono dim">{duration(alert.duration_seconds)}</span>
      {alert.suppressed && <Stamp className="stamp-small">已抑制</Stamp>}
    </p>)}
    {group.session && <Row className="monitor-known">
      <Link to={sessionPath(group.session.session_id)}>
        处理会话 {group.session.session_id}{group.session.pending_approval && " · 待审批"}
      </Link>
    </Row>}
  </div>);
}

const probeStates: Record<string, string> = { up: "可用", down: "失败", missing: "状态未取到" };

function probeRows({ applications }: PanelData<"probes">, ask: Ask) {
  return applications.map(({ application, targets }) => <div className="monitor-subgroup" key={application}>
    <Stamp>{application}</Stamp>
    {targets.map(({ address, availability, state, http_status: httpStatus, duration_seconds: seconds, points }) => {
      const status = probeStates[state] ?? probeStates.missing;
      const signal = httpStatus != null ? `HTTP ${httpStatus}`
        : seconds != null && Number.isFinite(seconds) ? `${Math.round(seconds * 1000)} ms` : "—";
      const question = `${application} 的 ${address} 这一小时可用率是多少，最近${state === "down" ? "探测失败的原因是什么" : "的探测情况如何"}？`;
      return <Question key={address} ask={ask} question={question}>
        <span className="mono">{address}</span>
        <span className="mono">{status} · {signal} · {percent(availability)}</span>
        <Sparkline points={points} down={state === "down" || (availability != null && availability < 1)} />
      </Question>;
    })}
  </div>);
}

function changeRows({ items }: PanelData<"changes">, ask: Ask) {
  return items.map((item) => {
    const rollback = item.kind === "vigil_rollback";
    const kind = rollback ? "回滚" : "发布";
    const at = time(item.occurred_at);
    const actor = rollback && item.approved_by ? ` · 批准人 ${item.approved_by}` : "";
    const object = item.namespace ? `${item.namespace}/${item.service}` : item.service;
    return <Question key={JSON.stringify([object, item.occurred_at, item.kind, item.version])}
      ask={ask} question={`${object} 在 ${at} 的这次${kind}改了什么？`}>
      <span className="mono">{item.service} · {item.version} · {at} · {kind}{actor}</span>
    </Question>;
  });
}

function usage(value?: number, unit?: string) {
  if (value == null || !Number.isFinite(value)) return "—";
  return unit === "bytes" ? `${Math.round(value / 1048576)} Mi` : `${Math.round(value * 1000)} mCPU`;
}

function issueText(issue: Issue) {
  switch (issue.kind) {
    case "replicas_not_ready": return `副本就绪 ${display(issue.ready)} / ${display(issue.desired)}`;
    case "no_ready_pods": return "没有就绪的 Pod";
    case "pod_pending": return `${issue.pod} · Pending`;
    case "frequent_restarts":
      return `近 1 小时重启 ${issue.estimated ? "约 " : ""}${issue.restart_count_last_hour} 次${issue.occurred_at ? ` · 最近一次 ${time(issue.occurred_at)}` : ""}`;
    default: return issue.reason || issue.kind;
  }
}

const serviceKey = (item: NamedService) => `${item.kind}/${item.name}`;

function ServiceQuestion({ item, ask }: { item: AbnormalService; ask: Ask }) {
  const issues = item.issues.filter((issue) => issue.kind !== "resource_near_limit");
  const resources = item.issues.filter((issue) => issue.kind === "resource_near_limit");
  const restart = issues.find((issue) => issue.kind === "frequent_restarts");
  let question = `${item.name} 当前为什么出现${issues.map(issueText).join("、")}？`;
  if (restart) {
    const recent = restart.occurred_at ? `，最近一次在 ${time(restart.occurred_at)}` : "";
    question = `${item.name} 近 1 小时为什么重启了 ${restart.estimated ? "约 " : ""}${restart.restart_count_last_hour} 次${recent}？`;
  } else if (!issues.length) {
    question = `${item.name} 的${resources[0]?.resource === "memory" ? "内存" : "CPU"}用量为什么接近限额？`;
  }
  return <Question ask={ask} question={question}>
    <strong className="mono">{item.name}</strong>
    {issues.map((issue, index) => <span key={`issue-${index}`} className="mono">{issueText(issue)}</span>)}
    {item.last_exits?.map((exit, index) => <span key={`exit-${index}`} className="dim mono">
      上次退出 · {display(exit.reason)} · 退出码 {display(exit.exit_code)} · {time(exit.occurred_at)}
    </span>)}
    {resources.map((signal, index) => <span key={`resource-${index}`} className="warn mono">
      {signal.container} · {signal.resource === "memory" ? "内存" : "CPU"} {usage(signal.usage, signal.unit)}
      {" / 限额 "}{usage(signal.limit, signal.unit)}（{percent(signal.ratio)}），快到上限
    </span>)}
    {!!item.missing?.length && <span className="dim">部分数据未取到</span>}
  </Question>;
}

function serviceRows({ abnormal, healthy, unknown }: PanelData<"services">, ask: Ask, fold: FoldState) {
  return [
    ...abnormal.map((item) => <ServiceQuestion key={serviceKey(item)} item={item} ask={ask} />),
    ...unknown.map((item) => <Row className="mono dim" key={serviceKey(item)}>{item.name} · 数据未取到</Row>),
    ...(!healthy.length ? [] : [abnormal.length
      ? <Fold key="healthy-fold" {...fold} label={`其余 ${healthy.length} 个服务正常`} names={healthy.map(({ name }) => name)} />
      : <Row key="healthy" className="mono">{healthy.length} 个服务正常</Row>]),
  ];
}

function warningRows({ items }: PanelData<"warnings">, ask: Ask) {
  return items.map(({ reason, object, message, count, last_seen: lastSeen }, index) => {
    const who = [object.kind, object.name].filter(Boolean).join(" ");
    return <Question key={`${who}/${reason}/${index}`} ask={ask}
      question={`${who} 在 ${time(lastSeen)} 的 ${reason} 告警需要处理吗？`}>
      <span className="warn mono">{reason}</span>
      <span className="mono">{who} · {time(lastSeen)} · {display(count)} 次</span>
      <span>{message}</span>
    </Question>;
  });
}

function nodeRows({ abnormal, unknown, healthy }: PanelData<"nodes">, fold: FoldState) {
  return [
    ...abnormal.map(({ name, conditions }) => {
      const why = conditions.map(({ condition, status }) => condition === "Ready"
        ? status === "unknown" ? "Ready 状态未知" : "NotReady" : condition);
      return <Row key={name} className="mono">{name} · {why.join(" · ")}</Row>;
    }),
    ...unknown.map((item) => <Row key={item.name} className="mono dim">
      {item.name} · 数据未取到（{item.missing_conditions.join("、")}）
    </Row>),
    ...(!healthy.length ? [] : [abnormal.length
      ? <Fold key="healthy-fold" {...fold} label={`其余 ${healthy.length} 台节点正常`} names={healthy} />
      : <Row key="healthy" className="mono">{healthy.length} 台节点正常</Row>]),
  ];
}

function grafanaUrl(value?: string) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

function panelRows(snapshot: Snapshot | undefined, ask: Ask, folds: Record<"services" | "nodes", FoldState>) {
  return {
    alerts: snapshot?.alerts.data && alertRows(snapshot.alerts.data),
    probes: snapshot?.probes.data && probeRows(snapshot.probes.data, ask),
    changes: snapshot?.changes.data && changeRows(snapshot.changes.data, ask),
    services: snapshot?.services.data && serviceRows(snapshot.services.data, ask, folds.services),
    warnings: snapshot?.warnings.data && warningRows(snapshot.warnings.data, ask),
    nodes: snapshot?.nodes.data && nodeRows(snapshot.nodes.data, folds.nodes),
  };
}

export function Monitoring({ ask, collapsed, toggle }: { ask: Ask; collapsed: boolean; toggle: () => void }) {
  const [{ snapshot, failed }, setData] = useState<{ snapshot?: Snapshot; failed?: boolean }>({});
  const [servicesOpen, setServicesOpen] = useState(false);
  const [nodesOpen, setNodesOpen] = useState(false);
  const poll = useCallback(async (signal: AbortSignal) => {
    try {
      const snapshot = await request<Snapshot>("/api/monitoring", { signal });
      if (!signal.aborted) setData({ snapshot });
    } catch {
      if (!signal.aborted) setData({ failed: true });
    }
  }, []);
  usePolling(poll, 5000);

  const count = snapshot?.abnormal_service_count;
  const color = count == null ? "dim" : count === 0 ? "ok" : "danger";
  const label = count == null ? "异常服务数未取到" : count === 0 ? "全部服务正常" : "个异常服务";
  const rows = panelRows(snapshot, ask, {
    services: { open: servicesOpen, setOpen: setServicesOpen },
    nodes: { open: nodesOpen, setOpen: setNodesOpen },
  });

  return <aside className="monitoring" aria-labelledby="monitoring-title">
    <header className="monitoring-head">
      <h2 id="monitoring-title" className="stamp">值班面板</h2>
      <button id="monitor-toggle" className="disclosure" type="button" onClick={toggle}
        aria-controls="monitoring-content" aria-expanded={!collapsed}
        aria-label={collapsed ? "展开值班面板" : "收起值班面板"}>{collapsed ? "[+]" : "[−]"}</button>
    </header>
    <div id="monitoring-content" className="monitoring-content" hidden={collapsed}>
      <div id="monitoring-hero" className="hero"><strong className={color}>{display(count)}</strong><span>{label}</span></div>
      <div id="monitoring-blocks">
        {(Object.keys(titles) as PanelKey[]).map((key) => {
          const url = key !== "warnings" && grafanaUrl(snapshot?.grafana_links?.[key]);
          const block = snapshot?.[key];
          const detail = [...new Set([...(block?.missing ?? []), ...(block?.failed ?? [])])];
          const unavailable = `数据未取到${detail.length ? ` · ${detail.join(" · ")}` : ""}`;
          return <section className="monitor-block" data-block={key} key={key}>
            <header>
              <h3 className="stamp">{titles[key]}</h3>
              {url && <a className="stamp grafana" href={url} target="_blank" rel="noopener noreferrer">Grafana ↗</a>}
            </header>
            {rows[key]?.length ? rows[key] : block?.status === "ok" && <Row className="dim">暂无记录</Row>}
            {block?.status !== "ok" && <Row className="dim">{snapshot || failed ? unavailable : "读取中…"}</Row>}
          </section>;
        })}
      </div>
    </div>
  </aside>;
}
