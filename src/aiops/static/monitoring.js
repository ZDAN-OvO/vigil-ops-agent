import { button, byId, disclosure, display, duration, element, sessionPath, stamp, svgNs, time } from "./ui.js";

const titles = {
  alerts: "正在触发的告警",
  probes: "入口探测",
  changes: "最近变更 · 24 小时",
  services: "异常服务",
  warnings: "Warning 事件 · 1 小时",
  nodes: "节点",
};

const row = (text, className = "") => element("p", `monitor-row ${className}`, text);
const percent = (ratio) => Number.isFinite(ratio) ? `${(ratio * 100).toFixed(0)}%` : "—";

function sparkline(points, down) {
  const group = element("span", "spark-group");
  group.append(stamp("近 1 小时", "stamp-small"));
  const series = points ?? [];
  if (series.some(([, y]) => y === 0 || y === 1)) {
    const svg = document.createElementNS(svgNs, "svg");
    svg.setAttribute("viewBox", "0 0 60 16");
    svg.setAttribute("class", `spark ${down ? "danger" : "ok"}`);
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(svgNs, "path");
    const commands = [];
    const isSample = (index) => [0, 1].includes(series[index]?.[1]);
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
    path.setAttribute("d", commands.join(" "));
    svg.append(path);
    group.append(svg);
  } else {
    group.append(element("span", "spark mono dim", "—"));
  }
  group.append(stamp("可用率", "stamp-small"));
  return group;
}

function alertRows(data) {
  const rows = [];
  for (const group of data.groups) {
    const block = element("div", "monitor-subgroup");
    for (const alert of group.alerts) {
      const current = element("p", "monitor-row monitor-alert");
      if (!alert.suppressed) current.append(element("span", "flag"));
      current.append(element("span", "mono", group.name));
      const severity = alert.severity === "critical" ? "danger" : alert.severity === "warning" ? "warn" : "dim";
      current.append(stamp(alert.severity || "—", severity));
      current.append(element("span", "mono dim", duration(alert.duration_seconds)));
      if (alert.suppressed) current.append(stamp("已抑制", "stamp-small"));
      block.append(current);
    }
    if (group.session) {
      const { session_id: id, pending_approval: pending } = group.session;
      const link = element("a", "", `处理会话 ${id}${pending ? " · 待审批" : ""}`);
      link.href = sessionPath(id);
      link.dataset.focusKey = `alert-session-${id}`;
      const linkRow = row("", "monitor-known");
      linkRow.append(link);
      block.append(linkRow);
    }
    rows.push(block);
  }
  return rows;
}

function probeRows(data, ask) {
  return data.applications.map(({ application, targets }) => {
    const group = element("div", "monitor-subgroup");
    group.append(stamp(application));
    for (const target of targets) {
      const { address, availability, state, http_status: httpStatus, duration_seconds: seconds } = target;
      const status = { up: "可用", down: "失败", missing: "状态未取到" }[state] ?? "状态未取到";
      const signal = httpStatus != null ? `HTTP ${httpStatus}`
        : Number.isFinite(seconds) ? `${Math.round(seconds * 1000)} ms` : "—";
      const question = `${application} 的 ${address} 这一小时可用率是多少，最近${state === "down" ? "探测失败的原因是什么" : "的探测情况如何"}？`;
      group.append(ask(question,
        element("span", "mono", address),
        element("span", "mono", `${status} · ${signal} · ${percent(availability)}`),
        sparkline(target.points, state === "down" || (availability != null && availability < 1)),
      ));
    }
    return group;
  });
}

function changeRows(data, ask) {
  return data.items.map((item) => {
    const rollback = item.kind === "vigil_rollback";
    const kind = rollback ? "回滚" : "发布";
    const at = time(item.occurred_at);
    const actor = rollback && item.approved_by ? ` · 批准人 ${item.approved_by}` : "";
    const object = item.namespace ? `${item.namespace}/${item.service}` : item.service;
    return ask(`${object} 在 ${at} 的这次${kind}改了什么？`,
      element("span", "mono", `${item.service} · ${item.version} · ${at} · ${kind}${actor}`));
  });
}

function usage(value, unit) {
  if (!Number.isFinite(value)) return "—";
  return unit === "bytes" ? `${Math.round(value / 1048576)} Mi` : `${Math.round(value * 1000)} mCPU`;
}

function issueText(issue) {
  switch (issue.kind) {
    case "replicas_not_ready": return `副本就绪 ${display(issue.ready)} / ${display(issue.desired)}`;
    case "no_ready_pods": return "没有就绪的 Pod";
    case "pod_pending": return `${issue.pod} · Pending`;
    case "frequent_restarts":
      return `近 1 小时重启 ${issue.estimated ? "约 " : ""}${issue.restart_count_last_hour} 次${issue.occurred_at ? ` · 最近一次 ${time(issue.occurred_at)}` : ""}`;
    default: return issue.reason || issue.kind;
  }
}

function serviceRows(data, ask, fold) {
  const rows = [];
  for (const item of data.abnormal) {
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
    const description = issues.map((issue) => element("span", "mono", issueText(issue)));
    for (const exit of item.last_exits ?? []) {
      description.push(element("span", "dim mono",
        `上次退出 · ${display(exit.reason)} · 退出码 ${display(exit.exit_code)} · ${time(exit.occurred_at)}`));
    }
    for (const signal of resources) {
      const label = signal.resource === "memory" ? "内存" : "CPU";
      description.push(element("span", "warn mono",
        `${signal.container} · ${label} ${usage(signal.usage, signal.unit)} / 限额 ${usage(signal.limit, signal.unit)}（${percent(signal.ratio)}），快到上限`));
    }
    if (item.missing?.length) description.push(element("span", "dim", "部分数据未取到"));
    rows.push(ask(question, element("strong", "mono", item.name), ...description));
  }
  for (const item of data.unknown) rows.push(row(`${item.name} · 数据未取到`, "mono dim"));
  if (data.healthy.length) {
    rows.push(data.abnormal.length
      ? fold("services", `其余 ${data.healthy.length} 个服务正常`, data.healthy.map(({ name }) => name))
      : row(`${data.healthy.length} 个服务正常`, "mono"));
  }
  return rows;
}

function warningRows(data, ask) {
  return data.items.map(({ reason, object, message, count, last_seen: lastSeen }) => {
    const who = [object.kind, object.name].filter(Boolean).join(" ");
    return ask(`${who} 在 ${time(lastSeen)} 的 ${reason} 告警需要处理吗？`,
      element("span", "warn mono", reason),
      element("span", "mono", `${who} · ${time(lastSeen)} · ${display(count)} 次`),
      element("span", "", message));
  });
}

function nodeRows(data, fold) {
  const rows = data.abnormal.map(({ name, conditions }) => {
    const why = conditions.map(({ condition, status }) => condition === "Ready"
      ? status === "unknown" ? "Ready 状态未知" : "NotReady"
      : condition);
    return row(`${name} · ${why.join(" · ")}`, "mono");
  });
  for (const item of data.unknown) {
    rows.push(row(`${item.name} · 数据未取到（${item.missing_conditions.join("、")}）`, "mono dim"));
  }
  if (data.healthy.length) {
    rows.push(data.abnormal.length
      ? fold("nodes", `其余 ${data.healthy.length} 台节点正常`, data.healthy)
      : row(`${data.healthy.length} 台节点正常`, "mono"));
  }
  return rows;
}

function grafanaUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

export function createMonitoring(fillQuestion) {
  const hero = byId("monitoring-hero");
  const blocks = byId("monitoring-blocks");
  const toggle = byId("monitor-toggle");
  const panelContent = byId("monitoring-content");
  const open = new Set();
  let current;

  function updateToggle() {
    const expanded = !panelContent.hidden;
    toggle.textContent = expanded ? "[−]" : "[+]";
    toggle.setAttribute("aria-label", expanded ? "收起值班面板" : "展开值班面板");
    toggle.setAttribute("aria-expanded", String(expanded));
  }

  updateToggle();
  toggle.addEventListener("click", () => {
    panelContent.hidden = !panelContent.hidden;
    toggle.closest(".workspace").classList.toggle("monitor-collapsed", panelContent.hidden);
    updateToggle();
  });

  function ask(question, ...content) {
    const trigger = button("monitor-row monitor-question", "", () => fillQuestion(question));
    trigger.dataset.focusKey = question;
    const data = element("span");
    data.append(...content);
    const hint = stamp("↵ 提问", "ask-label gold");
    hint.setAttribute("aria-hidden", "true");
    trigger.append(data, hint);
    return trigger;
  }

  function fold(key, label, names) {
    const row = element("div", "monitor-row");
    const part = disclosure(label, `monitor-fold-${key}`);
    part.toggle.dataset.focusKey = `折叠 ${key}`;
    part.setOpen(open.has(key));
    part.toggle.addEventListener("click", () => {
      if (part.body.inert) open.delete(key);
      else open.add(key);
    });
    part.content.append(...names.map((name) => element("p", "mono dim monitor-known", name)));
    row.append(part.toggle, element("span", "mono", label), part.body);
    return row;
  }

  const renderRows = {
    alerts: alertRows, probes: probeRows, changes: changeRows,
    services: serviceRows, warnings: warningRows, nodes: (data) => nodeRows(data, fold),
  };

  function render(snapshot, failed = false) {
    const { collected_at: _, ...view } = snapshot ?? {};
    const fingerprint = JSON.stringify([view, failed]);
    if (current === fingerprint) return;
    current = fingerprint;

    const active = blocks.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = active?.dataset.focusKey;
    const count = snapshot?.abnormal_service_count;
    const color = count == null ? "dim" : count === 0 ? "ok" : "danger";
    const label = count == null ? "异常服务数未取到" : count === 0 ? "全部服务正常" : "个异常服务";
    hero.replaceChildren(element("strong", color, display(count)), element("span", "", label));

    const sections = [];
    for (const [key, title] of Object.entries(titles)) {
      const section = element("section", "monitor-block");
      section.dataset.block = key;
      const header = element("header");
      header.append(element("h3", "stamp", title));
      const grafana = key !== "warnings" && grafanaUrl(snapshot?.grafana_links?.[key]);
      if (grafana) {
        const link = element("a", "stamp grafana", "Grafana ↗");
        link.href = grafana;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.dataset.focusKey = `grafana-${key}`;
        header.append(link);
      }
      section.append(header);
      const block = snapshot?.[key];
      if (!block || failed || !block.data) {
        section.append(row(snapshot || failed ? "数据未取到" : "读取中…", "dim"));
      } else {
        const data = renderRows[key](block.data, ask, fold);
        if (data.length) section.append(...data);
        else if (block.status === "ok") section.append(row("暂无记录", "dim"));
        if (block.status !== "ok") {
          const detail = [...(block.missing ?? []), ...(block.failed ?? [])];
          section.append(row(`数据未取到${detail.length ? ` · ${[...new Set(detail)].join(" · ")}` : ""}`, "dim"));
        }
      }
      sections.push(section);
    }
    blocks.replaceChildren(...sections);
    if (focusKey) {
      const focus = [...blocks.querySelectorAll("[data-focus-key]")].find((item) => item.dataset.focusKey === focusKey);
      focus?.focus({ preventScroll: true });
    }
  }

  render(null);
  return { render };
}
