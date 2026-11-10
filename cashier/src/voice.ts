// Optional Mongolian amount readout (tenant setting, default off) from pre-recorded word clips.
// The words are generated here from the parsed amount, never from the email's amount-in-words.
// Grammar (to confirm with a native speaker, PLAN Q30): before "төгрөг" every unit, ten and
// hundred takes its attributive form (таван, хорин, зуун); мянга is "мянган" only when it is the
// last word; сая and тэрбум never change.

const UNITS = ["", "нэг", "хоёр", "гурван", "дөрвөн", "таван", "зургаан", "долоон", "найман", "есөн"];
const TENS = ["", "арван", "хорин", "гучин", "дөчин", "тавин", "жаран", "далан", "наян", "ерэн"];

function below1000(n: number, words: string[], omitLeadingOne: boolean): void {
  const hundreds = Math.floor(n / 100);
  const tens = Math.floor((n % 100) / 10);
  const units = n % 10;
  if (hundreds > 0) {
    if (!(hundreds === 1 && omitLeadingOne)) words.push(UNITS[hundreds] ?? "");
    words.push("зуун");
  }
  if (tens > 0) words.push(TENS[tens] ?? "");
  if (units > 0) words.push(UNITS[units] ?? "");
}

export function numberWords(value: number): string[] {
  if (!Number.isInteger(value) || value <= 0 || value >= 1e12) return [];
  const words: string[] = [];
  const billions = Math.floor(value / 1e9);
  const millions = Math.floor((value % 1e9) / 1e6);
  const thousands = Math.floor((value % 1e6) / 1e3);
  const rest = value % 1e3;
  if (billions) { below1000(billions, words, false); words.push("тэрбум"); }
  if (millions) { below1000(millions, words, false); words.push("сая"); }
  if (thousands) { below1000(thousands, words, true); if (thousands === 1 && words.at(-1) === "нэг") words.pop(); words.push("мянга"); }
  if (rest) below1000(rest, words, true);
  if (words.at(-1) === "мянга") words[words.length - 1] = "мянган";
  return words;
}

export type ClipManifest = Record<string, string>;

export class Voice {
  constructor(private readonly manifest: ClipManifest | null) {}

  get available(): boolean {
    return this.manifest !== null;
  }

  /** Speak "<amount> төгрөг орлоо" for whole MNT amounts at or below the threshold. */
  async speak(amount: string, currency: string, maxAmount: string | null): Promise<boolean> {
    if (!this.manifest || currency !== "MNT") return false;
    const value = Number(amount);
    if (!Number.isFinite(value) || !Number.isInteger(value)) return false;
    if (maxAmount !== null && value > Number(maxAmount)) return false;
    const words = [...numberWords(value), "төгрөг", "орлоо"];
    const files = words.map((w) => this.manifest?.[w]);
    if (files.some((f) => !f)) return false;
    for (const file of files) await playClip(`/voice/${file}`);
    return true;
  }
}

function playClip(src: string): Promise<void> {
  return new Promise((resolve) => {
    const audio = new Audio(src);
    audio.addEventListener("ended", () => resolve(), { once: true });
    audio.addEventListener("error", () => resolve(), { once: true });
    audio.play().catch(() => resolve());
  });
}

export async function loadVoice(): Promise<Voice> {
  try {
    const r = await fetch("/voice/manifest.json", { cache: "no-cache" });
    return new Voice(r.ok ? ((await r.json()) as ClipManifest) : null);
  } catch {
    return new Voice(null);
  }
}
