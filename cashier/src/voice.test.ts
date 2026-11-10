import { numberWords, Voice } from "./voice";

describe("numberWords (Mongolian, attributive before төгрөг)", () => {
  it.each([
    [2800, "хоёр мянга найман зуун"],
    [15000, "арван таван мянган"],
    [120, "зуун хорин"],
    [100, "зуун"],
    [1000, "мянган"],
    [1100, "мянга зуун"],
    [101, "зуун нэг"],
    [1_000_000, "нэг сая"],
    [2_500_000, "хоёр сая таван зуун мянган"],
    [999_999, "есөн зуун ерэн есөн мянга есөн зуун ерэн есөн"],
    [3_000_000_005, "гурван тэрбум таван"],
  ])("%i", (n, words) => {
    expect(numberWords(n).join(" ")).toBe(words);
  });
  it("rejects non-integers and out-of-range values", () => {
    expect(numberWords(0)).toEqual([]);
    expect(numberWords(1.5)).toEqual([]);
    expect(numberWords(1e12)).toEqual([]);
  });
});

describe("Voice", () => {
  it("is unavailable without clips and never speaks decimals, other currencies or large amounts", async () => {
    const none = new Voice(null);
    expect(none.available).toBe(false);
    expect(await none.speak("2800", "MNT", null)).toBe(false);
    const v = new Voice({});
    expect(await v.speak("2800.50", "MNT", null)).toBe(false);
    expect(await v.speak("250", "USD", null)).toBe(false);
    expect(await v.speak("90000", "MNT", "50000")).toBe(false);
    expect(await v.speak("2800", "MNT", null)).toBe(false); // clips missing for these words
  });
});
