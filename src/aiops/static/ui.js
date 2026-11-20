export const byId = (id) => document.getElementById(id);

export function element(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

export function button(className, text, action) {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

export function stamp(text, className = "") {
  return element("span", `stamp ${className}`, text);
}

export const svgNs = "http://www.w3.org/2000/svg";

export function spinner() {
  const node = document.createElementNS(svgNs, "svg");
  node.setAttribute("class", "spinner");
  node.setAttribute("aria-hidden", "true");
  node.setAttribute("viewBox", "0 0 8 8");
  const track = document.createElementNS(svgNs, "circle");
  track.setAttribute("cx", "4");
  track.setAttribute("cy", "4");
  track.setAttribute("r", "3.5");
  const arc = document.createElementNS(svgNs, "path");
  arc.setAttribute("d", "M4 .5 A3.5 3.5 0 0 1 7.5 4");
  node.append(track, arc);
  return node;
}

export const display = (value) => {
  if (value == null || value === "") return "—";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
};

export const time = (value, seconds = false) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", ...(seconds && { second: "2-digit" }), hourCycle: "h23",
  });
};

export function duration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const whole = Math.max(0, Math.floor(seconds));
  const min = Math.floor(whole / 60);
  if (min >= 60) return `${Math.floor(min / 60)} 小时 ${min % 60} 分`;
  return min ? `${min} 分 ${whole % 60} 秒` : `${whole} 秒`;
}

export function disclosure(label, id) {
  const body = element("div", "fold-body");
  body.id = id;
  body.inert = true;
  const content = element("div");
  body.append(content);
  const toggle = button("disclosure", "[+]", () => setOpen(body.inert));
  toggle.setAttribute("aria-label", label);
  toggle.setAttribute("aria-controls", id);
  function setOpen(open) {
    body.inert = !open;
    body.classList.toggle("open", open);
    toggle.textContent = open ? "[−]" : "[+]";
    toggle.setAttribute("aria-expanded", String(open));
  }
  setOpen(false);
  return { body, content, toggle, setOpen };
}

export const sessionPath = (id) => `/sessions/${encodeURIComponent(id)}`;

export async function request(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...(options.body !== undefined && {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(options.body),
    }),
    method: options.method,
  });
  let data;
  try {
    data = await res.json();
  } catch {
    throw Object.assign(new Error("服务响应格式无效"), { status: res.status });
  }
  if (!res.ok) {
    throw Object.assign(new Error(data.message || "服务请求失败"), {
      code: data.error, status: res.status,
    });
  }
  return data;
}
