import vectors from "../../shared/format_vectors.json";
import { formatAmount, parseAmountInput, relativeTime } from "./format";

describe("formatAmount (shared vectors with the server)", () => {
  for (const v of vectors.amounts) {
    it(`${v.amount} ${v.currency} -> ${v.text}`, () => {
      expect(formatAmount(v.amount, v.currency)).toBe(v.text);
    });
  }
  it("leaves malformed input unchanged", () => {
    expect(formatAmount("abc", "MNT")).toBe("abc");
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-10-15T00:40:00Z");
  it.each([
    ["2026-10-15T00:39:50Z", "Дөнгөж сая"],
    ["2026-10-15T00:38:00Z", "2 минутын өмнө"],
    ["2026-10-14T22:40:00Z", "2 цагийн өмнө"],
    ["2026-10-12T00:40:00Z", "3 өдрийн өмнө"],
  ])("%s", (then, text) => {
    expect(relativeTime(new Date(then), now)).toBe(text);
  });
});

describe("parseAmountInput", () => {
  it.each([
    ["2800", "2800"],
    ["2 800", "2800"],
    ["2,800.50 ₮", "2800.50"],
    ["0", null],
    ["abc", null],
    ["1.234", null],
  ])("%s", (raw, expected) => {
    expect(parseAmountInput(raw)).toBe(expected);
  });
});
