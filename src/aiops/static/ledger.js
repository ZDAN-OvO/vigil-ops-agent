import { button, element, spinner, stamp } from "./ui.js";

const summaries = {
  session: () => "会话开始或恢复",
  session_closed: () => "会话关闭",
  message: (event) => event.source === "alert" ? "告警消息" : event.role === "user" ? "调查问题" : "Agent 回复",
  tool_call: (event) => event.summary || "工具调用",
  hypothesis: () => "更新假设",
  evidence: () => "关联证据",
  input_delivered: (event) => event.lane === "side" ? "旁路接收" : "调查提示送达",
  approval_required: () => "等待人工审批",
  card_decision: (event) => event.status === "approved" ? "批准决定" : "拒绝决定",
  action_result: () => "动作回执",
  result: () => "轮次结果",
};

export function createLedger(root) {
  const windowNode = root.querySelector(".ledger-window");
  const previous = root.querySelector(".ledger-prev");
  const popup = root.querySelector(".event-popover");
  popup.id = "event-popover";
  let entries = [];
  let offset = 0;
  let capacity = 1;
  let sessionState;
  let gap = false;
  let trigger;
  let dismissed;
  let pointerInPopup = false;
  const cells = new Map();

  function hide() {
    if (trigger) trigger.setAttribute("aria-expanded", "false");
    trigger = undefined;
    popup.hidden = true;
    pointerInPopup = false;
  }

  function position() {
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const inset = 8;
    const viewport = document.documentElement.clientWidth;
    popup.style.maxWidth = `min(560px, ${viewport - 2 * inset}px)`;
    const width = popup.offsetWidth;
    const left = rect.left + width <= viewport - inset ? rect.left : rect.right - width;
    popup.style.left = `${Math.max(inset, left)}px`;
    popup.style.top = `${rect.bottom}px`;
  }

  function show(cell, event) {
    if (dismissed === cell) return;
    if (trigger && trigger !== cell) trigger.setAttribute("aria-expanded", "false");
    trigger = cell;
    popup.textContent = JSON.stringify(event, null, 2);
    popup.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    position();
  }

  function settlePopup() {
    if (!trigger || pointerInPopup || trigger.matches(":hover")) return;
    if (document.activeElement === trigger || popup.contains(document.activeElement)) return;
    hide();
  }

  function makeCell(event) {
    const cell = button("ledger-cell", "", () => {
      dismissed = undefined;
      show(cell, event);
      popup.focus({ preventScroll: true });
    });
    cell.dataset.seq = event.seq;
    cell.setAttribute("aria-controls", popup.id);
    cell.setAttribute("aria-expanded", "false");
    const summary = element("span", "ledger-summary");
    summary.append(element("span", "", summaries[event.type]?.(event) ?? event.type));
    cell.append(stamp(`SEQ ${String(event.seq).padStart(4, "0")}`), summary);
    const running = spinner();
    running.setAttribute("hidden", "");
    summary.append(running);
    cell.addEventListener("focus", () => show(cell, event));
    cell.addEventListener("pointerenter", () => {
      dismissed = undefined;
      show(cell, event);
    });
    cell.addEventListener("pointerleave", (event) => {
      if (event.relatedTarget !== popup) settlePopup();
    });
    cell.addEventListener("blur", () => {
      if (dismissed === cell) dismissed = undefined;
      queueMicrotask(settlePopup);
    });
    return { cell, running };
  }

  const historySlots = () => Math.max(1, capacity - 1 - Number(gap));
  const maxOffset = () => Math.max(0, entries.length - 1 - historySlots());

  function render() {
    const latest = entries.at(-1);
    root.hidden = !latest;
    if (!latest) return;

    const activeCell = windowNode.contains(document.activeElement) ? document.activeElement : null;
    const activeSeq = Number(activeCell?.dataset.seq);
    const focusInPopup = document.activeElement === popup;
    const focusedEarlier = document.activeElement === previous;
    offset = Math.min(offset, maxOffset());

    const end = entries.length - 1 - offset;
    const view = entries.slice(Math.max(0, end - historySlots()), end).concat(latest);
    const visible = new Set(view.map((event) => event.seq));
    if (trigger && !visible.has(Number(trigger.dataset.seq))) hide();
    for (const [seq, { cell }] of cells) {
      if (visible.has(seq)) continue;
      cell.remove();
      cells.delete(seq);
    }

    previous.hidden = maxOffset() === 0;
    previous.disabled = offset >= maxOffset();
    root.style.setProperty("--ledger-width", `${100 / capacity}%`);

    for (const [index, event] of view.entries()) {
      if (!cells.has(event.seq)) cells.set(event.seq, makeCell(event));
      const { cell, running } = cells.get(event.seq);
      cell.classList.toggle("current", event === latest);
      if (event === latest) cell.setAttribute("aria-current", "true");
      else cell.removeAttribute("aria-current");
      running.toggleAttribute("hidden", event !== latest || sessionState !== "working");
      if (windowNode.children[index] !== cell) {
        windowNode.insertBefore(cell, windowNode.children[index] ?? null);
      }
    }
    if (gap && !windowNode.querySelector(".ledger-next")) {
      const next = element("span", "ledger-cell ledger-next mono", "— — —");
      next.setAttribute("aria-label", "下一步等待审批");
      windowNode.append(next);
    } else if (!gap) {
      windowNode.querySelector(".ledger-next")?.remove();
    }
    if ((activeCell && document.activeElement !== activeCell) || (focusInPopup && popup.hidden)) {
      const next = cells.get(activeSeq)?.cell ?? cells.get(view[0].seq).cell;
      next.focus({ preventScroll: true });
    } else if (focusedEarlier && previous.disabled) {
      root.focus({ preventScroll: true });
    }
    position();
  }

  function shift(delta) {
    const next = Math.max(0, Math.min(maxOffset(), offset + delta));
    if (offset === next) return;
    offset = next;
    render();
  }

  previous.addEventListener("click", () => shift(1));
  popup.addEventListener("pointerenter", () => { pointerInPopup = true; });
  popup.addEventListener("pointerleave", () => { pointerInPopup = false; settlePopup(); });
  popup.addEventListener("blur", () => queueMicrotask(settlePopup));
  root.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && trigger) {
      event.preventDefault();
      const cell = trigger;
      dismissed = cell;
      hide();
      cell.focus({ preventScroll: true });
    }
    if (event.key === "Tab" && trigger && event.target === trigger && !event.shiftKey) {
      event.preventDefault();
      popup.focus({ preventScroll: true });
    } else if (event.key === "Tab" && event.target === popup && trigger) {
      const anchor = trigger;
      if (event.shiftKey) {
        event.preventDefault();
        anchor.focus({ preventScroll: true });
      } else {
        // 执行浏览器正常的下一个 Tab，从锚点继续。
        dismissed = anchor;
        hide();
        anchor.focus({ preventScroll: true });
      }
    }
    if (event.target !== popup && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
      event.preventDefault();
      shift(event.key === "ArrowLeft" ? 1 : -1);
    }
  });
  // 浮层也可由悬停打开，焦点在账本外时 Esc 同样可以关闭。
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && trigger && !root.contains(event.target)) {
      event.preventDefault();
      const cell = trigger;
      dismissed = cell;
      hide();
      cell.focus({ preventScroll: true });
    }
  });

  new ResizeObserver(() => {
    const width = root.clientWidth;
    if (!width) return;
    const next = Math.max(3, Math.floor((width - 70) / 115));
    if (capacity !== next) {
      capacity = next;
      render();
    }
    position();
  }).observe(root);

  function update(nextEntries, state, mainBlocked) {
    if (offset && entries.length && nextEntries !== entries) offset += nextEntries.length - entries.length;
    entries = nextEntries;
    sessionState = state;
    gap = mainBlocked;
    render();
  }

  return { update };
}
