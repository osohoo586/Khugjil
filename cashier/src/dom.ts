// The only way UI code creates elements. Text is always inserted as text nodes; there is no
// innerHTML anywhere (ESLint forbids it and the CSP enforces Trusted Types).
type Child = Node | string | number | null | undefined | false;
type AttrValue = string | number | boolean | undefined | EventListener;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Record<string, AttrValue> = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
    if (key.startsWith("on")) {
      if (typeof value !== "function") throw new TypeError("event handlers must be functions");
      el.addEventListener(key.slice(2), value);
    } else if (key === "class") {
      el.className = String(value);
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, ...children);
  return el;
}

export function append(el: Element, ...children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    el.append(typeof child === "string" || typeof child === "number" ? String(child) : child);
  }
}

export function replace(el: Element, ...children: Child[]): void {
  el.replaceChildren();
  append(el, ...children);
}
