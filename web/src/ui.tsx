import { type ReactNode } from "react";
import type { Session, SessionState } from "./api";

export const sessionStates: Record<SessionState, string> = {
  idle: "待提问", working: "运行中", awaiting_approval: "待审批",
  interrupted: "已中断", closed: "已关闭",
};

export const sessionSources: Record<Session["source"], string> = {
  user: "人工", alert: "告警", inspection: "巡检",
};

export function sessionState(session: Session) {
  return session.source === "inspection" && session.state === "closed"
    ? "已结束" : sessionStates[session.state] ?? "状态未取到";
}

export function Stamp({ className = "", children }: { className?: string; children: ReactNode }) {
  return <span className={`stamp ${className}`}>{children}</span>;
}

export function Spinner({ hidden = false }: { hidden?: boolean }) {
  return <svg className="spinner" aria-hidden="true" data-hidden={hidden || undefined} viewBox="0 0 8 8">
    <circle cx="4" cy="4" r="3.5" />
    <path d="M4 .5 A3.5 3.5 0 0 1 7.5 4" />
  </svg>;
}

export function display(value: unknown): string {
  if (value == null || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function time(value?: string | null, seconds = false): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", ...(seconds && { second: "2-digit" }), hourCycle: "h23",
  });
}

export function duration(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const whole = Math.max(0, Math.floor(seconds));
  const min = Math.floor(whole / 60);
  if (min >= 60) return `${Math.floor(min / 60)} 小时 ${min % 60} 分`;
  return min ? `${min} 分 ${whole % 60} 秒` : `${whole} 秒`;
}
