// Tiny WebAudio synth for UI/event sounds — no audio files needed.
let ctx: AudioContext | null = null;
let enabled = true;
let volume = 0.6;

function ac(): AudioContext {
  if (!ctx) ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
  return ctx;
}

export const sound = {
  configure(on: boolean, vol: number) {
    enabled = on;
    volume = vol;
  },
  play(type: "signal" | "win" | "loss" | "click" | "alert") {
    if (!enabled) return;
    try {
      const a = ac();
      const now = a.currentTime;
      const notes: Record<string, number[]> = {
        signal: [660, 880],
        win: [523, 659, 784],
        loss: [330, 220],
        click: [520],
        alert: [880, 660, 880],
      };
      (notes[type] || [440]).forEach((f, i) => {
        const o = a.createOscillator();
        const g = a.createGain();
        o.type = type === "win" ? "triangle" : "sine";
        o.frequency.value = f;
        const t = now + i * 0.08;
        g.gain.setValueAtTime(0, t);
        g.gain.linearRampToValueAtTime(volume * 0.25, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.22);
        o.connect(g).connect(a.destination);
        o.start(t);
        o.stop(t + 0.24);
      });
    } catch {
      /* ignore */
    }
  },
};
