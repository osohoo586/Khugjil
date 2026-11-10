// Flash the document title ("(1) Орлого 2,800 ₮") until the page gets focus.
export class TitleFlasher {
  private timer: ReturnType<typeof setInterval> | null = null;
  private unseen = 0;
  private message = "";
  private on = false;

  constructor(
    private readonly base: string,
    private readonly doc: Document = document,
    private readonly win: Window = window,
  ) {
    this.win.addEventListener("focus", () => this.stop());
  }

  notify(text: string): void {
    if (this.doc.hasFocus()) return;
    this.unseen += 1;
    this.message = `(${this.unseen}) ${text}`;
    this.doc.title = this.message;
    if (this.timer === null) {
      this.timer = setInterval(() => {
        this.on = !this.on;
        this.doc.title = this.on ? this.base : this.message;
      }, 1000);
    }
  }

  stop(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    this.unseen = 0;
    this.doc.title = this.base;
  }
}
