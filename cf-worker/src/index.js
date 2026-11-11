// Cloudflare Email Worker: the fallback inbound path when the server cannot receive mail on port 25
// (PLAN Q4, threat model TB2/S5/I10). Cloudflare Email Routing hands each message for
// <secret>@in.<DOMAIN> to this Worker, which forwards the raw MIME, unchanged and only in memory,
// to POST https://app.<DOMAIN>/hooks/cf. The server then runs the same checks as for SMTP,
// including DKIM/ARC. Nothing is logged or stored here.
//
// Signature (must match server/src/khugjil/api_v1/hooks.py):
//   hex HMAC-SHA256(secret, "<unix ts>\n<rcpt>\n<sha256 hex of body>")

export const MAX_MESSAGE_BYTES = 256 * 1024; // same limit as the SMTP server

const encoder = new TextEncoder();
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export async function sign(secret, ts, rcpt, body) {
  const bodyHash = hex(await crypto.subtle.digest("SHA-256", body));
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, encoder.encode(`${ts}\n${rcpt}\n${bodyHash}`)));
}

/** Outcome for a hook response: "done" (accepted, dropped or duplicate), "reject" or "retry". */
export function outcome(status) {
  if (status === 202 || status === 409) return "done"; // 409: this exact delivery was already taken
  if (status === 413) return "reject";
  return "retry"; // 401/404 (misconfigured secret), 429, 5xx: let the sender retry later
}

export default {
  async email(message, env) {
    if (!env.KHJ_CF_INGEST_SECRET || !env.KHJ_HOOK_URL) throw new Error("not configured");
    if (message.rawSize > MAX_MESSAGE_BYTES) {
      message.setReject("Message too large");
      return;
    }
    const body = new Uint8Array(await new Response(message.raw).arrayBuffer());
    if (body.byteLength > MAX_MESSAGE_BYTES) {
      message.setReject("Message too large");
      return;
    }
    const ts = Math.floor(Date.now() / 1000).toString();
    const rcpt = message.to;
    const response = await fetch(env.KHJ_HOOK_URL, {
      method: "POST",
      body,
      redirect: "manual",
      headers: {
        "content-type": "message/rfc822",
        "x-khj-timestamp": ts,
        "x-khj-rcpt": rcpt,
        "x-khj-signature": await sign(env.KHJ_CF_INGEST_SECRET, ts, rcpt, body),
      },
    });
    switch (outcome(response.status)) {
      case "done":
        return;
      case "reject":
        message.setReject("Message too large");
        return;
      default:
        // Throwing makes Email Routing answer with a temporary failure, so Gmail retries.
        throw new Error(`hook status ${response.status}`);
    }
  },
};
