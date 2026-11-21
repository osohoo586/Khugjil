import vectors from "../../shared/format_vectors.json";
import { dayLabel, formatAmount, parseAmountInput, relativeTime } from "./format";

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
    ["2026-10-15T00:38:00Z", "2 мин өмнө"],
    ["2026-10-14T22:40:00Z", "2 цаг өмнө"],
    ["2026-10-12T00:40:00Z", "3 өдөр өмнө"],
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

describe("dayLabel", () => {
  it("shows the Ulaanbaatar date and weekday", () => {
    // 2026-11-20 16:30 UTC is already Saturday 00:30 in Ulaanbaatar (UTC+8).
    expect(dayLabel(new Date("2026-11-20T16:30:00Z"))).toBe("2026.11.21, Бямба");
    expect(dayLabel(new Date("2026-11-20T15:59:00Z"))).toBe("2026.11.20, Баасан");
  });
});
