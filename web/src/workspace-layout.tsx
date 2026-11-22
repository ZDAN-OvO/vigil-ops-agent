import {
  type KeyboardEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState,
} from "react";

const key = "vigil.workspace.layout";
const defaults = { sessions: 240, monitor: 360, monitorCollapsed: false };
const min = { sessions: 180, monitor: 240 };
const max = { sessions: 360, monitor: 520 };
const conversationMin = 480;
const divider = 8;
const collapsedWidth = 42;
type Panel = "sessions" | "monitor";
type Layout = typeof defaults;

function clamp(value: number, low: number, high: number) {
  return Math.min(Math.max(value, low), high);
}

function readLayout(): Layout {
  try {
    const saved = JSON.parse(localStorage.getItem(key) ?? "null");
    if (Number.isFinite(saved?.sessions) && Number.isFinite(saved?.monitor) &&
      saved.sessions >= min.sessions && saved.sessions <= max.sessions &&
      saved.monitor >= min.monitor && saved.monitor <= max.monitor &&
      typeof saved.monitorCollapsed === "boolean") {
      return { sessions: saved.sessions, monitor: saved.monitor, monitorCollapsed: saved.monitorCollapsed };
    }
  } catch {
    // 存储不可用时仍可在当前页面调整。
  }
  return defaults;
}

function viewportWidth() {
  return document.documentElement.clientWidth || window.innerWidth;
}

function pixelWidth(n: number) {
  return `${Number.isFinite(n) ? Math.max(0, n) : 0}px`;
}

function PanelDivider({ panel, value, upper, resize }: {
  panel: Panel; value: number; upper: number; resize: (width: number) => void;
}) {
  const drag = useRef<{ id: number; x: number; start: number }>(null);
  const [dragging, setDragging] = useState(false);
  const direction = panel === "sessions" ? 1 : -1;

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const width = {
      ArrowLeft: value - direction * 16, ArrowRight: value + direction * 16,
      Home: min[panel], End: upper,
    }[event.key];
    if (typeof width !== "number") return;
    event.preventDefault();
    resize(width);
  }

  return <div className={`panel-divider ${panel}-divider`} data-dragging={dragging || undefined}
    role="separator" tabIndex={0} aria-orientation="vertical"
    aria-label={panel === "sessions" ? "调整会话栏宽度" : "调整值班面板宽度"}
    aria-controls={panel === "sessions" ? "sessions-panel" : "monitor-panel"}
    aria-valuemin={min[panel]} aria-valuemax={upper} aria-valuenow={value}
    onKeyDown={onKeyDown} onPointerDown={(event) => {
      if (event.button !== 0 || drag.current) return;
      event.currentTarget.focus();
      event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = { id: event.pointerId, x: event.clientX, start: value };
      setDragging(true);
    }} onPointerMove={(event) => {
      const start = drag.current;
      if (start?.id === event.pointerId) resize(start.start + direction * (event.clientX - start.x));
    }} onLostPointerCapture={(event) => {
      if (drag.current?.id === event.pointerId) {
        drag.current = null;
        setDragging(false);
      }
    }} />;
}

export function WorkspaceLayout({ children, sessions, monitoring }: {
  children: ReactNode;
  sessions: (close: () => void) => ReactNode;
  monitoring: (collapsed: boolean, toggle: () => void, close: () => void) => ReactNode;
}) {
  const [layout, setLayout] = useState(readLayout);
  const [width, setWidth] = useState(viewportWidth);
  const [overlay, setOverlay] = useState<Panel>();
  const main = useRef<HTMLElement>(null);
  const sessionsButton = useRef<HTMLButtonElement>(null);
  const overlayLauncher = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const resize = () => setWidth(viewportWidth());
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);

  useEffect(() => {
    try { localStorage.setItem(key, JSON.stringify(layout)); } catch { /* 存储可能不可用。 */ }
  }, [layout]);

  const sessionsDocked = width >= conversationMin + min.sessions + divider + collapsedWidth;
  const monitorFits = width >= conversationMin + min.sessions + min.monitor + 2 * divider;
  const monitorDocked = monitorFits && !layout.monitorCollapsed;
  const space = width - conversationMin - (monitorDocked ? 2 * divider : divider + collapsedWidth);
  const left = sessionsDocked
    ? clamp(layout.sessions, min.sessions, monitorDocked ? space - min.monitor : space)
    : layout.sessions;
  const right = monitorDocked ? clamp(layout.monitor, min.monitor, space - left) : layout.monitor;
  const leftLimit = Math.min(max.sessions, space - (monitorDocked ? right : 0));
  const rightLimit = Math.min(max.monitor, space - left);
  const overlayVisible = overlay === "sessions" && !sessionsDocked
    || overlay === "monitor" && !monitorFits ? overlay : undefined;

  useLayoutEffect(() => {
    const element = main.current;
    if (!element) return;
    element.style.setProperty("--sessions-width", pixelWidth(left));
    element.style.setProperty("--monitor-width", pixelWidth(right));
  }, [left, right]);

  function closeOverlay() {
    setOverlay(undefined);
    const button = overlayLauncher.current;
    overlayLauncher.current = null;
    if (button?.isConnected) button.focus();
  }

  function showOverlay(panel: Panel) {
    overlayLauncher.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOverlay(panel);
  }

  useLayoutEffect(() => {
    if (overlayVisible === "sessions") sessionsButton.current?.focus();
    if (overlayVisible === "monitor") {
      main.current?.querySelector<HTMLButtonElement>("#monitor-toggle")?.focus();
    }
    if (overlay && !overlayVisible) setOverlay(undefined);
  }, [overlay, overlayVisible]);

  function toggleMonitor() {
    if (monitorFits) {
      setLayout((current) => ({ ...current, monitorCollapsed: !current.monitorCollapsed }));
    } else if (overlayVisible === "monitor") {
      closeOverlay();
    } else {
      showOverlay("monitor");
    }
  }

  return <main ref={main} className={monitorDocked ? "workspace" : "workspace monitor-collapsed"}
    data-sessions-docked={sessionsDocked} data-overlay={overlayVisible}
    onKeyDown={(event) => {
      if (event.key === "Escape" && overlayVisible && !event.defaultPrevented) {
        event.preventDefault();
        closeOverlay();
      }
    }}>
    <div className="workspace-controls">
      {!sessionsDocked && <button type="button" className="session-control" aria-controls="sessions-panel"
        aria-expanded={overlayVisible === "sessions"} onClick={() => showOverlay("sessions")}>会话</button>}
      {!sessionsDocked && <button type="button" className="session-control"
        aria-controls="monitor-panel" aria-expanded={overlayVisible === "monitor"}
        onClick={toggleMonitor}>值班面板</button>}
      <button type="button" className="session-control" onClick={() => {
        setLayout(defaults);
        closeOverlay();
      }}>恢复默认分栏</button>
    </div>
    <div id="sessions-panel" className="sessions-panel" hidden={!sessionsDocked && overlayVisible !== "sessions"}>
      {!sessionsDocked && <button type="button" className="session-control overlay-close" ref={sessionsButton}
        onClick={closeOverlay}>关闭会话栏</button>}
      {sessions(() => { if (overlayVisible === "sessions") closeOverlay(); })}
    </div>
    {sessionsDocked && <PanelDivider panel="sessions" value={left} upper={leftLimit}
      resize={(value) => setLayout((current) => ({ ...current, sessions: clamp(value, min.sessions, leftLimit) }))} />}
    {children}
    {monitorDocked && <PanelDivider panel="monitor" value={right} upper={rightLimit}
      resize={(value) => setLayout((current) => ({ ...current, monitor: clamp(value, min.monitor, rightLimit) }))} />}
    <div id="monitor-panel" className="monitor-panel" hidden={!sessionsDocked && overlayVisible !== "monitor"}>
      {monitoring(!monitorDocked && overlayVisible !== "monitor", toggleMonitor, () => {
        if (overlayVisible === "monitor") closeOverlay();
      })}
    </div>
    {overlayVisible && <button type="button" className="panel-backdrop" aria-label="关闭侧栏" onClick={closeOverlay} />}
  </main>;
}
