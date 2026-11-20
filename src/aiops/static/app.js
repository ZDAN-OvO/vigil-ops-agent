import { createCard } from "./cards.js";
import { createLedger } from "./ledger.js";
import { createMonitoring } from "./monitoring.js";
import { button, byId, disclosure, element, request, sessionPath, spinner, stamp, time } from "./ui.js";

const states = {
  idle: "待提问", working: "运行中", awaiting_approval: "待审批",
  interrupted: "已中断", closed: "已关闭",
};
const decode = (value) => {
  try { return decodeURIComponent(value); } catch { return value; }
};
const route = location.pathname.match(/^\/sessions\/([^/]+)\/?$/);
const id = route ? decode(route[1]) : null;
const api = id ? `/api${sessionPath(id)}` : null;
const conversation = byId("conversation-scroll");
const stream = byId("event-stream");
const question = byId("question");
const send = byId("send");
const newSession = byId("new-session");
const notice = byId("session-notice");
const composeNotice = byId("compose-notice");
const ledger = createLedger(byId("ledger"));
const monitoring = createMonitoring(fillQuestion);

let session;
let access = {};
let events = [];
let cursor = 0;
let lastMinute;
let anchored;
let historyReady = false;
let sessionError = "";
let sendInFlight = false;
let messageAttempt;
let queuedInput;
let sessionAction;
let listAccess;
let creating = false;
let sessionListVersion;
let actionVersion;
let refreshTask;
let refreshQueued = false;
const seen = new Set();
const cards = new Map();
const observations = new Map();
const evidence = new Map();
const deliveries = new Map();
const messages = new Map();

function showNotice() {
  if (sessionError) {
    notice.textContent = sessionError;
    return;
  }
  const link = decode(location.hash.slice(1));
  notice.textContent = historyReady && link.startsWith("card-") && !document.getElementById(link)
    ? "此会话中找不到链接指向的卡片" : "";
}

function locateCard() {
  const link = decode(location.hash.slice(1));
  if (anchored === link) return;
  if (anchored) document.getElementById(anchored)?.classList.remove("anchor-target");
  const card = link.startsWith("card-") && document.getElementById(link);
  if (!card) {
    anchored = undefined;
    showNotice();
    return;
  }
  anchored = link;
  card.classList.add("anchor-target");
  card.scrollIntoView({ block: "start" });
  card.focus({ preventScroll: true });
  showNotice();
}

function updateComposer() {
  question.disabled = false;
  send.disabled = !session || session.state === "closed" || !access.approval_enabled
    || !question.value.trim() || sendInFlight;
}

function fillQuestion(text) {
  question.value = text;
  if (!session) composeNotice.textContent = "选择会话后可发送";
  else if (session.state === "closed") composeNotice.textContent = "此会话已关闭";
  else if (!access.approval_enabled) composeNotice.textContent = "只读 · 当前无法发送调查问题";
  else composeNotice.textContent = "";
  updateComposer();
  question.focus();
}

question.addEventListener("input", updateComposer);
byId("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (send.disabled) return;
  const text = question.value.trim();
  // 请求失败但可能已入账，同一草稿重试沿用输入 ID。
  if (messageAttempt?.text !== text) messageAttempt = { text, input_id: crypto.randomUUID() };
  sendInFlight = true;
  updateComposer();
  composeNotice.textContent = "正在投递…";
  try {
    const response = await request(`${api}/messages`, { method: "POST", body: messageAttempt });
    if (question.value.trim() === text) question.value = "";
    messageAttempt = undefined;
    queuedInput = response.input_id;
    composeNotice.textContent = messages.has(queuedInput) ? "" : "已排队 · 等待账本更新";
    void refresh();
  } catch (error) {
    composeNotice.textContent = error.message;
  } finally {
    sendInFlight = false;
    updateComposer();
  }
});

function renderSessions(items) {
  const list = byId("session-list");
  const version = JSON.stringify([items, listAccess.approval_enabled]);
  if (sessionListVersion === version) return;
  sessionListVersion = version;
  const activeKey = list.contains(document.activeElement) ? document.activeElement.dataset.focusKey : "";
  const rows = items.map((item) => {
    const selected = item.id === id;
    const row = element("div", `session-row${selected ? " selected" : ""}`);
    const link = element("a", "session-link");
    link.href = sessionPath(item.id);
    link.dataset.focusKey = `session-${item.id}`;
    if (selected) link.setAttribute("aria-current", "page");
    if (item.pending_approval) {
      const flag = element("span", "flag");
      flag.setAttribute("aria-label", "有待审批卡片");
      row.append(flag);
    }
    link.append(element("span", "session-name", item.title || item.id),
      stamp(item.source === "alert" ? "告警" : "人工", "stamp-small"),
      stamp(states[item.state] ?? "状态未取到", `stamp-small ${item.state === "working" ? "gold" : ""}`));
    row.append(link);
    if (item.state === "interrupted") {
      const control = button("stamp stamp-small session-resume", "接手恢复", () => changeSession(item.id, "resume", control));
      control.dataset.focusKey = `resume-${item.id}`;
      control.disabled = !listAccess.approval_enabled;
      row.append(control);
    } else if (item.state === "working") {
      row.append(spinner());
    }
    return row;
  });
  list.replaceChildren(...rows);
  if (activeKey) [...list.querySelectorAll("[data-focus-key]")]
    .find((item) => item.dataset.focusKey === activeKey)?.focus({ preventScroll: true });
  byId("sessions-notice").textContent = items.length ? "" : "暂无会话";
}

async function loadSessions() {
  try {
    const result = await request("/api/sessions");
    listAccess = result;
    newSession.disabled = creating || !listAccess.approval_enabled;
    byId("sessions-access").textContent = listAccess.approval_enabled ? ""
      : `只读 · ${listAccess.approval_disabled_reason || "当前无法创建会话"}`;
    renderSessions(result.sessions);
  } catch (error) {
    byId("sessions-notice").textContent = error.message;
    sessionListVersion = undefined;
  }
}

newSession.addEventListener("click", async () => {
  if (creating) return;
  creating = true;
  newSession.disabled = true;
  const status = byId("sessions-notice");
  status.textContent = "正在创建会话…";
  try {
    const { session: created } = await request("/api/sessions", { method: "POST" });
    location.assign(sessionPath(created.id));
  } catch (error) {
    status.textContent = error.message;
    creating = false;
    newSession.disabled = !listAccess?.approval_enabled;
  }
});

async function changeSession(target, action, control) {
  if (sessionAction) return;
  sessionAction = action;
  control.disabled = true;
  const status = target === id ? composeNotice : byId("sessions-notice");
  status.textContent = action === "close" ? "正在关闭…" : "正在恢复…";
  try {
    const response = await request(`/api${sessionPath(target)}/${action}`, { method: "POST" });
    if (target !== id) {
      location.assign(sessionPath(target));
      return;
    }
    showSession(response.session);
    status.textContent = action === "close" ? "会话已关闭" : "已提交恢复";
    void refresh();
    void loadSessions();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    sessionAction = undefined;
    control.disabled = false;
  }
}

function showSession(next) {
  if (session && next.last_seq < session.last_seq) return;
  session = next;
  const label = states[session.state] ?? "状态未取到";
  byId("session-title").textContent = session.title || session.id;
  byId("session-meta").textContent =
    `SESSION ${session.id} · ${session.source === "alert" ? "告警" : "人工"} · ${label}`;
  byId("session-meta").dataset.state = session.state;
  const version = JSON.stringify([session.source, session.state, access]);
  if (version !== actionVersion) {
    actionVersion = version;
    const actions = byId("session-actions");
    actions.replaceChildren();
    if (session.source === "alert" && session.state !== "closed") {
      const control = button("session-control", "关闭告警会话", () => changeSession(id, "close", control));
      control.disabled = !access.approval_enabled;
      actions.append(control);
    }
    if (!access.approval_enabled) actions.append(stamp(access.approval_disabled_reason || "只读"));
  }
  updateComposer();
  updateLedger();
}

function updateLedger() {
  ledger.update(events, session?.state, session?.state === "awaiting_approval");
}

function updateDelivery(inputId) {
  const message = messages.get(inputId);
  const delivered = deliveries.get(inputId);
  if (!message || !delivered) return;
  if (delivered.lane === "side") {
    message.textContent = "旁路 · 未送入主线";
  } else if (Number.isInteger(delivered.step)) {
    message.textContent = `已送入 · 第 ${delivered.step} 步`;
  }
}

function renderMessage(event) {
  const alert = event.source === "alert";
  const user = event.role === "user" && !alert;
  const message = element("div", `message${user ? " user-message" : ""}`);
  message.append(stamp(alert ? "ALERT" : user ? "你" : "AGENT"),
    element("p", "", event.text));
  if (event.input_id) {
    const delivery = element("p", "stamp delivery");
    message.append(delivery);
    messages.set(event.input_id, delivery);
    updateDelivery(event.input_id);
    if (event.input_id === queuedInput && composeNotice.textContent.startsWith("已排队")) {
      composeNotice.textContent = "";
    }
  }
  return message;
}

function renderObservation(event) {
  const observation = element("section", "observation");
  const head = element("header", "observation-head");
  const part = disclosure("展开或收起观测", `observation-details-${event.seq}`);
  const tool = event.tool?.replaceAll("_", " ") || "查询";
  head.append(stamp(`OBSERVATION · ${tool} · ${time(event.collected_at, true)}`),
    part.toggle);
  const details = event.observation;
  const text = details == null ? "观测详情未取到"
    : typeof details === "string" ? details : JSON.stringify(details, null, 2);
  part.content.append(element("pre", "", text));
  observation.append(head, element("p", "observation-summary", event.summary), part.body);
  if (event.observation_id) {
    observations.set(event.observation_id, {
      reveal() {
        part.setOpen(true);
        observation.scrollIntoView({ block: "nearest" });
        part.toggle.focus({ preventScroll: true });
      },
    });
  }
  return observation;
}

function appendEntry(event, node) {
  if (!node) return;
  const row = element("div", "feed-row");
  const entry = element("div", "feed-entry");
  entry.append(node);
  row.append(entry);
  const minute = Math.floor(Date.parse(event.ts) / 60000);
  if (minute !== lastMinute) {
    const when = element("time", "", time(event.ts));
    when.dateTime = event.ts;
    row.append(when);
    lastMinute = minute;
  }
  stream.append(row);
}

function renderEvent(event) {
  switch (event.type) {
    case "message":
      return renderMessage(event);
    case "tool_call":
      return renderObservation(event);
    case "hypothesis": {
      const row = element("p");
      const color = { supported: "ok", refuted: "danger" }[event.status] ?? "dim";
      row.append(stamp(`HYPOTHESIS ${event.id} · `), document.createTextNode(`${event.statement} → `),
        element("span", `stamp ${color}`, event.status));
      return row;
    }
    case "input_delivered":
      deliveries.set(event.input_id, event);
      updateDelivery(event.input_id);
      return null;
    case "evidence":
      if (event.evidence_id ?? event.id) evidence.set(event.evidence_id ?? event.id, event.observation_id);
      return null;
    case "approval_required": {
      const card = createCard(event, {
        observationFor: (key) => observations.get(evidence.get(key)),
        decide: async (proposal, decision) => {
          await request(`${api}/cards/${encodeURIComponent(proposal.card_id)}/decision`, {
            method: "POST",
            body: { fingerprint: proposal.fingerprint, decision },
          });
          void refresh();
          void loadSessions();
        },
      });
      cards.set(event.card_id, card);
      card.setAccess(access);
      return card.node;
    }
    case "card_decision":
    case "action_result":
      cards.get(event.card_id)?.apply(event);
      return null;
    case "session_closed":
      return element("p", "stamp", "会话已关闭");
    default:
      return null;
  }
}

function ingest(batch) {
  const fresh = batch.filter((event) => event.session_id === id && Number.isSafeInteger(event.seq))
    .sort((a, b) => a.seq - b.seq);
  const added = [];
  for (const event of fresh) {
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
    added.push(event);
  }
  if (!added.length) return;
  const pinBottom = events.length > 0 &&
    conversation.scrollHeight - conversation.scrollTop - conversation.clientHeight < 25;
  const previousTail = events.at(-1)?.seq ?? 0;
  events = events.concat(added).sort((a, b) => a.seq - b.seq);
  if (added[0].seq < previousTail) {
    // 迟到事件按账本顺序重放，避免较旧决定覆盖较新的执行回执。
    for (const index of [cards, observations, evidence, deliveries, messages]) index.clear();
    lastMinute = undefined;
    anchored = undefined;
    stream.replaceChildren();
    for (const event of events) appendEntry(event, renderEvent(event));
  } else {
    for (const event of added) appendEntry(event, renderEvent(event));
  }
  for (const card of cards.values()) card.updateEvidence();
  updateLedger();
  if (pinBottom && !location.hash) conversation.scrollTop = conversation.scrollHeight;
  locateCard();
}

async function loadSession() {
  try {
    const detail = await request(api);
    access = {
      approval_enabled: detail.approval_enabled,
      approval_disabled_reason: detail.approval_disabled_reason,
    };
    showSession(detail.session);
    for (const card of cards.values()) card.setAccess(access);
    let page;
    do {
      page = await request(`${api}/events?after_seq=${cursor}&limit=100`);
      const next = page.next_after_seq;
      if (!Number.isSafeInteger(next) || next < cursor || (page.has_more && next === cursor)) {
        throw new Error("事件账本续读位置无效");
      }
      ingest(page.events);
      cursor = next;
    } while (page.has_more);
    historyReady = cursor >= session.last_seq;
    sessionError = "";
    byId("connection").textContent = "";
  } catch (error) {
    sessionError = error.status === 404 ? "会话不存在" : error.message;
    byId("connection").textContent = error.status === 404 ? "" : "连接中断 · 正在重试";
  }
  showNotice();
}

function refresh() {
  if (!api) return Promise.resolve();
  if (refreshTask) {
    refreshQueued = true;
    return refreshTask;
  }
  refreshTask = (async () => {
    do {
      refreshQueued = false;
      await loadSession();
    } while (refreshQueued);
  })().finally(() => { refreshTask = undefined; });
  return refreshTask;
}

async function loadMonitoring() {
  try {
    monitoring.render(await request("/api/monitoring"));
  } catch {
    monitoring.render(null, true);
  }
}

function poll(action, delay) {
  async function run() {
    try {
      await action();
    } finally {
      window.setTimeout(run, delay);
    }
  }
  void run();
}

window.addEventListener("hashchange", locateCard);
updateComposer();
poll(loadSessions, 3000);
poll(refresh, 900);
poll(loadMonitoring, 5000);
window.setInterval(() => {
  const now = Date.now();
  for (const card of cards.values()) card.tick(now);
}, 1000);
