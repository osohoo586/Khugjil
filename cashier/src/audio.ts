// A short neutral two-tone chime synthesised with WebAudio (no audio file). Browsers only allow
// sound after a user gesture, so "Ээлж эхлүүлэх" (or the sound-locked bar) calls unlock().
export class Chime {
  private ctx: AudioContext | null = null;

  get unlocked(): boolean {
    return this.ctx !== null && this.ctx.state === "running";
  }

  async unlock(): Promise<boolean> {
    try {
      this.ctx ??= new AudioContext();
      if (this.ctx.state !== "running") await this.ctx.resume();
    } catch {
      return false;
    }
    return this.unlocked;
  }

  play(): void {
    const ctx = this.ctx;
    if (!ctx || ctx.state !== "running") return;
    const start = ctx.currentTime;
    [660, 880].forEach((frequency, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = frequency;
      const t = start + i * 0.14;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.13);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.14);
    });
  }
}
