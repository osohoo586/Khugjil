import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import worker, { MAX_MESSAGE_BYTES, outcome, sign } from "../src/index.js";

const vectors = JSON.parse(readFileSync(new URL("../../shared/cf_hook_vectors.json", import.meta.url), "utf8"));
const env = { KHJ_CF_INGEST_SECRET: "test-cf-secret-not-real", KHJ_HOOK_URL: "https://app.khugjil.test/hooks/cf" };

function fakeMessage(raw, to = "k7q2m4x9d3ta6vwe5hb2nrfy@in.khugjil.test") {
  const bytes = new TextEncoder().encode(raw);
  return {
    to,
    rawSize: bytes.byteLength,
    raw: new Response(bytes).body,
    rejected: null,
    setReject(reason) {
      this.rejected = reason;
    },
  };
}

function withFetch(status, fn) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status });
  };
  return fn(calls).finally(() => {
    globalThis.fetch = original;
  });
}

test("signature matches the server's (shared vector)", async () => {
  for (const v of vectors) {
    assert.equal(await sign(v.secret, v.ts, v.rcpt, new TextEncoder().encode(v.body)), v.signature);
  }
});

test("forwards the raw MIME unchanged with a fresh signature, logging nothing", async () => {
  const raw = vectors[0].body;
  const logged = [];
  const log = console.log;
  console.log = (...a) => logged.push(a);
  try {
    await withFetch(202, async (calls) => {
      const msg = fakeMessage(raw);
      await worker.email(msg, env);
      assert.equal(calls.length, 1);
      const { url, init } = calls[0];
      assert.equal(url, env.KHJ_HOOK_URL);
      assert.equal(init.method, "POST");
      assert.equal(init.redirect, "manual");
      assert.equal(new TextDecoder().decode(init.body), raw);
      const h = init.headers;
      assert.equal(h["x-khj-rcpt"], msg.to);
      assert.ok(Math.abs(Number(h["x-khj-timestamp"]) - Date.now() / 1000) < 5);
      assert.equal(h["x-khj-signature"], await sign(env.KHJ_CF_INGEST_SECRET, h["x-khj-timestamp"], msg.to, init.body));
      assert.equal(msg.rejected, null);
    });
  } finally {
    console.log = log;
  }
  assert.deepEqual(logged, []);
});

test("duplicates are fine, oversized mail is rejected, failures ask the sender to retry", async () => {
  assert.equal(outcome(202), "done");
  assert.equal(outcome(409), "done");
  assert.equal(outcome(413), "reject");
  for (const s of [400, 401, 404, 429, 500, 502, 503]) assert.equal(outcome(s), "retry", String(s));

  await withFetch(503, async () => {
    await assert.rejects(worker.email(fakeMessage("x"), env), /hook status 503/);
  });
  await withFetch(202, async (calls) => {
    const big = fakeMessage("x".repeat(MAX_MESSAGE_BYTES + 1));
    await worker.email(big, env);
    assert.equal(big.rejected, "Message too large");
    assert.equal(calls.length, 0);
  });
  await assert.rejects(worker.email(fakeMessage("x"), {}), /not configured/);
});
