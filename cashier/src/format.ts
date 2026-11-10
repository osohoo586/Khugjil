// Mirrors server/src/khugjil/delivery/format.py; both are tested against shared/format_vectors.json.

export function formatAmount(amount: string, currency: string): string {
  const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(amount.trim());
  if (!match) return amount;
  const [, sign = "", whole = "0", frac = ""] = match;
  const grouped = whole.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const cents = frac.padEnd(2, "0");
  const text = `${sign}${grouped}${cents === "00" ? "" : `.${cents}`}`;
  return currency === "MNT" ? `${text} ₮` : `${text} ${currency}`;
}

export function relativeTime(then: Date, now: Date): string {
  const seconds = Math.max(0, Math.round((now.getTime() - then.getTime()) / 1000));
  if (seconds < 45) return "Дөнгөж сая";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} минутын өмнө`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} цагийн өмнө`;
  return `${Math.round(hours / 24)} өдрийн өмнө`;
}

export function clock(now: Date): string {
  return new Intl.DateTimeFormat("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Ulaanbaatar",
  }).format(now);
}

/** Normalise what a cashier types into the verify box ("2 800", "2,800.00") to a decimal string. */
export function parseAmountInput(raw: string): string | null {
  const cleaned = raw.replace(/[\s,₮]/g, "");
  return /^\d{1,13}(\.\d{1,2})?$/.test(cleaned) && Number(cleaned) > 0 ? cleaned : null;
}
