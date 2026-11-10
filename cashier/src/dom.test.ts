import { h, replace } from "./dom";
import { hero, txnList, verifyResult } from "./ui/screens";
import type { Txn } from "./api";

const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';

export function txn(over: Partial<Txn> = {}): Txn {
  return {
    id: "t1", seq: 1, amount: "2800.00", currency: "MNT", amount_text: "2,800 ₮",
    description: "Худалдан авалт", txn_at: "2026-10-15T00:37:22Z", time_text: "08:37",
    datetime_text: "2026.10.15 08:37", account_alias: "Төв дэлгүүр", test: false, ...over,
  };
}

describe("untrusted descriptions are rendered as text only", () => {
  it("h() inserts strings as text nodes", () => {
    const el = h("p", {}, evil);
    expect(el.textContent).toBe(evil);
    expect(el.querySelector("img, script")).toBeNull();
  });
  it("hero, list and verify box never create elements from descriptions", () => {
    const t = txn({ description: evil });
    const root = h("div", {}, hero(t, true, new Date(), true), txnList([t], true));
    const out = h("p");
    verifyResult(out, [t], null);
    root.append(out);
    expect(root.querySelector("img, script")).toBeNull();
    expect(root.textContent).toContain(evil);
    expect((window as { pwned?: number }).pwned).toBeUndefined();
  });
  it("rejects string event handlers", () => {
    expect(() => h("p", { onclick: "alert(1)" as unknown as EventListener })).toThrow(TypeError);
  });
  it("replace() swaps children", () => {
    const el = h("div", {}, "a");
    replace(el, "b", null, false, 3);
    expect(el.textContent).toBe("b3");
  });
});

describe("screens", () => {
  it("list shows the account column only for multi-account tenants and truncates with a tooltip", () => {
    const list = txnList([txn()], false);
    expect(list.querySelectorAll("th")).toHaveLength(3);
    expect(list.querySelector("td.truncate")?.getAttribute("title")).toBe("Худалдан авалт");
    expect(txnList([txn()], true).querySelectorAll("th")).toHaveLength(4);
    expect(txnList([], false).textContent).toContain("хоосон");
  });
  it("verify box messages", () => {
    const out = h("p");
    verifyResult(out, [txn()], null);
    expect(out.textContent).toBe("Орсон байна — 08:37, Худалдан авалт");
    verifyResult(out, [], null);
    expect(out.textContent).toBe("Сүүлийн 15 минутад ийм дүнтэй орлого ороогүй байна");
    verifyResult(out, null, null);
    expect(out.textContent).toBe("Шалгаж байна…");
    verifyResult(out, null, "Алдаа");
    expect(out.className).toContain("error");
  });
  it("hero highlights new items and shows an empty state", () => {
    expect(hero(txn(), false, new Date(), true).className).toBe("hero new");
    expect(hero(null, false, new Date(), false).textContent).toContain("орлого ороогүй");
  });
});
