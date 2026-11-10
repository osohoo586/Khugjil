import { backoffDelay, LiveStream, type StreamHandlers } from "./stream";

class FakeSource {
  static last: FakeSource | null = null;
  listeners = new Map<string, ((e: MessageEvent<string>) => void)[]>();
  closed = false;
  constructor(readonly url: string) {
    FakeSource.last = this;
  }
  addEventListener(type: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  emit(type: string, data = "{}") {
    for (const fn of this.listeners.get(type) ?? []) fn(new MessageEvent(type, { data }));
  }
  close() {
    this.closed = true;
  }
}

function handlers(): StreamHandlers & { calls: string[] } {
  const calls: string[] = [];
  return {
    calls,
    onTxn: (t) => calls.push(`txn:${t.id}`),
    onTest: () => calls.push("test"),
    onShiftEnded: () => calls.push("shift_ended"),
    onRevoked: () => calls.push("revoked"),
    onSettings: () => calls.push("settings"),
    onStatus: (ok) => calls.push(`status:${ok}`),
    onReconnected: () => calls.push("reconnected"),
  };
}

describe("backoffDelay", () => {
  it("grows exponentially with jitter and caps at 30 s", () => {
    expect(backoffDelay(0, () => 0)).toBe(500);
    expect(backoffDelay(0, () => 1)).toBe(1000);
    expect(backoffDelay(3, () => 0)).toBe(4000);
    expect(backoffDelay(20, () => 1)).toBe(30_000);
  });
});

describe("LiveStream", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("dispatches events and reconnects with backoff, then reports the reconnect", () => {
    const h = handlers();
    const stream = new LiveStream(h, (url) => new FakeSource(url) as unknown as EventSource);
    stream.connect();
    const first = FakeSource.last as FakeSource;
    first.emit("open");
    first.emit("txn", JSON.stringify({ id: "a" }));
    first.emit("test");
    first.emit("shift_ended");
    first.emit("settings_changed");
    first.emit("error");
    expect(first.closed).toBe(true);
    vi.advanceTimersByTime(1000);
    const second = FakeSource.last as FakeSource;
    expect(second).not.toBe(first);
    second.emit("open");
    second.emit("device_revoked");
    expect(second.closed).toBe(true);
    expect(h.calls).toEqual([
      "status:true", "txn:a", "test", "shift_ended", "settings", "status:false",
      "reconnected", "status:true", "revoked",
    ]);
  });

  it("does not reconnect after close()", () => {
    const h = handlers();
    const stream = new LiveStream(h, (url) => new FakeSource(url) as unknown as EventSource);
    stream.connect();
    const first = FakeSource.last as FakeSource;
    stream.close();
    first.emit("error");
    vi.advanceTimersByTime(60_000);
    expect(FakeSource.last).toBe(first);
  });
});
