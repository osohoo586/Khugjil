import type { Txn } from "./api";

export interface StreamHandlers {
  onTxn(txn: Txn): void;
  onTest(txn: Partial<Txn>): void;
  onShiftEnded(): void;
  onRevoked(): void;
  onSettings(): void;
  onStatus(connected: boolean): void;
  onReconnected(): void;
}

export type SourceFactory = (url: string) => EventSource;

/** Exponential backoff with jitter: 0.5–1 × min(30 s, 1 s × 2^attempt). */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(30_000, 1_000 * 2 ** attempt);
  return base / 2 + random() * (base / 2);
}

export class LiveStream {
  private source: EventSource | null = null;
  private attempt = 0;
  private everOpened = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;

  constructor(
    private readonly handlers: StreamHandlers,
    private readonly factory: SourceFactory = (url) => new EventSource(url),
    private readonly url = "/api/v1/stream",
  ) {}

  connect(): void {
    this.closed = false;
    const source = this.factory(this.url);
    this.source = source;
    source.addEventListener("open", () => {
      if (this.everOpened && this.attempt > 0) this.handlers.onReconnected();
      this.everOpened = true;
      this.attempt = 0;
      this.handlers.onStatus(true);
    });
    source.addEventListener("error", () => {
      source.close();
      this.handlers.onStatus(false);
      if (!this.closed) this.schedule();
    });
    source.addEventListener("txn", (e) => this.handlers.onTxn(JSON.parse((e as MessageEvent<string>).data) as Txn));
    source.addEventListener("test", (e) => this.handlers.onTest(JSON.parse((e as MessageEvent<string>).data) as Partial<Txn>));
    source.addEventListener("shift_ended", () => this.handlers.onShiftEnded());
    source.addEventListener("settings_changed", () => this.handlers.onSettings());
    source.addEventListener("device_revoked", () => {
      this.close();
      this.handlers.onRevoked();
    });
  }

  private schedule(): void {
    const delay = backoffDelay(this.attempt);
    this.attempt += 1;
    this.timer = setTimeout(() => this.connect(), delay);
  }

  close(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.source?.close();
    this.source = null;
  }
}
