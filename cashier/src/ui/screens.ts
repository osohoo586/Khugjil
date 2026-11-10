import type { DeviceInfo, Txn } from "../api";
import { h, replace } from "../dom";
import { clock, relativeTime } from "../format";

export const LIST_LIMIT = 10;
export const HIGHLIGHT_MS = 10_000;

export function message(text: string, kind: "info" | "error" = "info"): HTMLElement {
  return h("p", { class: `notice notice-${kind}`, role: kind === "error" ? "alert" : "status" }, text);
}

export function topBar(
  info: DeviceInfo,
  now: Date,
  onEnd: (() => void) | null,
): { el: HTMLElement; status: HTMLElement; clockEl: HTMLElement } {
  const name = info.accounts.length ? info.accounts.join(" · ") : info.tenant_name;
  const status = h("span", { class: "status ok" }, "Холбогдсон");
  const clockEl = h("span", { class: "tabular" }, clock(now));
  const right = h("div", { class: "top-right" });
  if (info.shift) right.append(h("span", { class: "who" }, info.shift.display_name));
  right.append(clockEl);
  if (onEnd) right.append(h("button", { type: "button", class: "btn btn-secondary", onclick: onEnd }, "Ээлж дуусгах"));
  const el = h("header", { class: "top" }, h("span", { class: "shop" }, name), status, right);
  return { el, status, clockEl };
}

export function hero(latest: Txn | null, showAlias: boolean, now: Date, highlight: boolean): HTMLElement {
  if (!latest) {
    return h(
      "section",
      { class: "hero", "aria-label": "Сүүлийн орлого" },
      h("p", { class: "hero-label" }, "Сүүлийн орлого"),
      h("p", { class: "hero-empty" }, "Ээлж эхэлснээс хойш орлого ороогүй байна."),
    );
  }
  return h(
    "section",
    { class: highlight ? "hero new" : "hero", "aria-label": "Сүүлийн орлого" },
    h("p", { class: "hero-label" }, "Сүүлийн орлого"),
    h("p", { class: "hero-amount tabular" }, latest.amount_text),
    h("p", { class: "hero-desc" }, latest.description),
    h(
      "p",
      { class: "hero-time" },
      `${latest.time_text} · ${relativeTime(new Date(latest.txn_at), now)}`,
      showAlias && latest.account_alias ? ` · ${latest.account_alias}` : "",
    ),
  );
}

export function txnList(items: Txn[], showAccount: boolean): HTMLElement {
  if (items.length === 0) {
    return h("section", { class: "list" }, h("p", { class: "muted" }, "Жагсаалт хоосон байна. Шинэ орлого энд харагдана."));
  }
  const head = h("tr", {}, h("th", {}, "Цаг"), h("th", { class: "num" }, "Дүн"), h("th", {}, "Утга"));
  if (showAccount) head.append(h("th", {}, "Данс"));
  const body = h("tbody");
  for (const t of items.slice(0, LIST_LIMIT)) {
    const row = h(
      "tr",
      {},
      h("td", { class: "tabular" }, t.time_text),
      h("td", { class: "num tabular" }, t.amount_text),
      h("td", { class: "truncate", title: t.description }, t.description),
    );
    if (showAccount) row.append(h("td", {}, t.account_alias ?? ""));
    body.append(row);
  }
  return h("section", { class: "list" }, h("table", { class: "table" }, h("thead", {}, head), body));
}

export function verifyBox(onInput: (value: string, out: HTMLElement) => void): HTMLElement {
  const out = h("p", { class: "verify-result", "aria-live": "polite" });
  const input = h("input", {
    id: "verify",
    inputmode: "decimal",
    autocomplete: "off",
    placeholder: "Жишээ нь 2800",
    oninput: (e: Event) => onInput((e.target as HTMLInputElement).value, out),
  });
  return h(
    "section",
    { class: "verify" },
    h("label", { for: "verify", class: "verify-label" }, "Дүнгээр шалгах"),
    input,
    out,
  );
}

export function verifyResult(out: HTMLElement, matches: Txn[] | null, error: string | null): void {
  if (error) {
    replace(out, error);
    out.className = "verify-result error";
  } else if (matches === null) {
    replace(out, "Шалгаж байна…");
    out.className = "verify-result muted";
  } else if (matches.length) {
    const first = matches[0] as Txn;
    replace(out, `Орсон байна — ${first.time_text}, ${first.description}`);
    out.className = "verify-result ok";
  } else {
    replace(out, "Сүүлийн 15 минутад ийм дүнтэй орлого ороогүй байна");
    out.className = "verify-result none";
  }
}

export function shiftStart(
  info: DeviceInfo,
  onStart: (memberId: string, pin: string, out: HTMLElement) => void,
): HTMLElement {
  const out = h("div", { class: "form-error", "aria-live": "assertive" });
  if (info.members.length === 0) {
    return h(
      "section",
      { class: "card narrow" },
      h("h1", {}, "Ээлж эхлүүлэх"),
      message("Энэ төхөөрөмж дээр ээлж эхлүүлэх хэрэглэгч алга. Админ хэсгээс хэрэглэгчийг энэ төхөөрөмжийн бүлэгт нэмж, урилга илгээнэ үү."),
    );
  }
  const select = h("select", { id: "member", required: true }, ...info.members.map((m) => h("option", { value: m.id }, m.display_name)));
  const pin = h("input", { id: "pin", type: "password", inputmode: "numeric", autocomplete: "off", maxlength: 6, required: true });
  const form = h(
    "form",
    { class: "stack" },
    h("label", { for: "member", class: "field-label" }, "Хэрэглэгч"),
    select,
    h("label", { for: "pin", class: "field-label" }, "ПИН код"),
    pin,
    h("button", { type: "submit", class: "btn btn-primary" }, "Ээлж эхлүүлэх"),
    out,
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    onStart(select.value, pin.value, out);
    pin.value = "";
  });
  return h(
    "section",
    { class: "card narrow" },
    h("h1", {}, "Ээлж эхлүүлэх"),
    form,
    h("p", { class: "muted" }, "Ээлж эхлүүлснээр дуут дохио болон мэдэгдэл идэвхжинэ. Ээлж эхлээгүй үед энэ төхөөрөмжид мэдэгдэл ирэхгүй."),
  );
}

export function pairScreen(onSubmit: (code: string, out: HTMLElement) => void): HTMLElement {
  const out = h("div", { class: "form-error", "aria-live": "assertive" });
  const input = h("input", { id: "code", autocomplete: "off", autocapitalize: "characters", placeholder: "XXXX-XXXX-XXXX", maxlength: 20, required: true });
  const form = h(
    "form",
    { class: "stack" },
    h("label", { for: "code", class: "field-label" }, "Холбох код"),
    input,
    h("button", { type: "submit", class: "btn btn-primary" }, "Холбох"),
    out,
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    onSubmit(input.value, out);
  });
  return h(
    "section",
    { class: "card narrow" },
    h("h1", {}, "Төхөөрөмж холбох"),
    h("p", {}, "Админ хэсгийн «Төхөөрөмжүүд» хуудаснаас авсан 12 тэмдэгттэй кодыг оруулна уу. Код 10 минутын дотор нэг удаа ажиллана."),
    form,
  );
}

export function inviteScreen(
  tenant: string,
  name: string,
  consent: string,
  onAccept: (pin: string, pin2: string, agreed: boolean, out: HTMLElement) => void,
): HTMLElement {
  const out = h("div", { class: "form-error", "aria-live": "assertive" });
  const pin = h("input", { id: "pin1", type: "password", inputmode: "numeric", maxlength: 6, autocomplete: "off", required: true });
  const pin2 = h("input", { id: "pin2", type: "password", inputmode: "numeric", maxlength: 6, autocomplete: "off", required: true });
  const agree = h("input", { id: "agree", type: "checkbox", required: true });
  const form = h(
    "form",
    { class: "stack" },
    h("pre", { class: "doc" }, consent),
    h("label", { class: "check" }, agree, " Дээрх нөхцөлийг уншиж, зөвшөөрч байна."),
    h("label", { for: "pin1", class: "field-label" }, "Шинэ ПИН код (6 оронтой)"),
    pin,
    h("label", { for: "pin2", class: "field-label" }, "ПИН код дахин"),
    pin2,
    h("button", { type: "submit", class: "btn btn-primary" }, "Бүртгүүлэх"),
    out,
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    onAccept(pin.value, pin2.value, agree.checked, out);
  });
  return h("section", { class: "card narrow" }, h("h1", {}, `${tenant} — ${name}`), form);
}

export function revokedScreen(): HTMLElement {
  return h(
    "section",
    { class: "card narrow" },
    h("h1", {}, "Төхөөрөмжийн эрх цуцлагдсан"),
    h("p", {}, "Энэ төхөөрөмжийг админ цуцалсан байна. Дахин ашиглахын тулд админаас шинэ холбох код авна уу."),
    h("a", { href: "/pair", class: "btn btn-primary" }, "Холбох хуудас руу"),
  );
}
