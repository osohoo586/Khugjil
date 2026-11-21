import type { DeviceInfo } from "../api";
import { h } from "../dom";
import { clock } from "../format";

/** Items kept in memory (and fetched after a reconnect); the feed shows FEED_LIMIT of them. */
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
  const clockEl = h("span", { class: "top-clock" }, clock(now));
  const right = h("div", { class: "top-right" });
  // With PIN shifts the member on duty matters; without, the device name adds nothing.
  if (info.shift && info.pin_shifts) right.append(h("span", { class: "who" }, info.shift.display_name));
  right.append(clockEl);
  if (onEnd) right.append(h("button", { type: "button", class: "btn btn-secondary", onclick: onEnd }, "Ээлж дуусгах"));
  const el = h("header", { class: "top" }, h("span", { class: "shop" }, name), status, right);
  return { el, status, clockEl };
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

const PAIR_COPY = {
  web: {
    title: "Төхөөрөмж холбох",
    hint: "Админ хэсгийн «Төхөөрөмжүүд» хуудаснаас авсан 12 тэмдэгттэй кодыг оруулна уу. Код 10 минутын дотор нэг удаа ажиллана.",
  },
  agent: {
    title: "Энэ компьютерийг холбох",
    hint: "Админ хэсгийн «Төхөөрөмжүүд» хуудаснаас «Агент» төрлийн код үүсгээд энд оруулна уу. Код 10 минутын дотор нэг удаа ажиллана.",
  },
} as const;

export function pairScreen(kind: "web" | "agent", onSubmit: (code: string, out: HTMLElement) => void): HTMLElement {
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
    h("h1", {}, PAIR_COPY[kind].title),
    h("p", {}, PAIR_COPY[kind].hint),
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

export function revokedScreen(onPair: () => void): HTMLElement {
  return h(
    "section",
    { class: "card narrow" },
    h("h1", {}, "Төхөөрөмжийн эрх цуцлагдсан"),
    h("p", {}, "Энэ төхөөрөмжийг админ цуцалсан байна. Дахин ашиглахын тулд админаас шинэ холбох код авна уу."),
    h("button", { type: "button", class: "btn btn-primary", onclick: onPair }, "Холбох хуудас руу"),
  );
}
