/** Sounds played when an agent session starts needing you: made with Web Audio (no files), or a file of your own. */

export interface SoundChoice { id: string; label: string }
export const SOUNDS: SoundChoice[] = [
  { id: "off", label: "No sound" },
  { id: "chime", label: "Chime" },
  { id: "bell", label: "Bell" },
  { id: "ping", label: "Ping" },
  { id: "beep", label: "Beep beep" },
  { id: "marimba", label: "Marimba" },
  { id: "alert", label: "Alert" },
  { id: "custom", label: "My own sound…" },
];

const KEY = "waw.mon.sound";
const VOLUME_KEY = "waw.mon.volume";
const CUSTOM_KEY = "waw.mon.sound.custom";
export const MAX_CUSTOM_BYTES = 1024 * 1024;

export const savedSound = () => localStorage.getItem(KEY) ?? "off";
export const saveSound = (id: string) => localStorage.setItem(KEY, id);
export const savedVolume = () => Number(localStorage.getItem(VOLUME_KEY) ?? "0.7");
export const saveVolume = (v: number) => localStorage.setItem(VOLUME_KEY, String(v));
export const customSound = () => localStorage.getItem(CUSTOM_KEY);
export const customSoundName = () => localStorage.getItem(`${CUSTOM_KEY}.name`);
export const saveCustomSound = (dataUrl: string, name: string) => {
  localStorage.setItem(CUSTOM_KEY, dataUrl);
  localStorage.setItem(`${CUSTOM_KEY}.name`, name);
};

let ctx: AudioContext | null = null;
function audio(): AudioContext | null {
  if (!ctx) {
    const C = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!C) return null;
    ctx = new C();
  }
  if (ctx.state === "suspended") void ctx.resume();
  return ctx;
}

// browsers only let a page make sound after the user interacted with it: unlock the audio on the first click / key
if (typeof window !== "undefined") {
  const unlock = () => { audio(); window.removeEventListener("pointerdown", unlock, true); window.removeEventListener("keydown", unlock, true); };
  window.addEventListener("pointerdown", unlock, true);
  window.addEventListener("keydown", unlock, true);
}

/** One note: `freq` Hz from `at` seconds for `len` seconds, fading out. */
function tone(c: AudioContext, out: AudioNode, freq: number, at: number, len: number, type: OscillatorType = "sine", peak = 1) {
  const o = c.createOscillator();
  const g = c.createGain();
  o.type = type;
  o.frequency.value = freq;
  const t = c.currentTime + at;
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(peak, t + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, t + len);
  o.connect(g).connect(out);
  o.start(t);
  o.stop(t + len + 0.05);
}

const SYNTH: Record<string, (c: AudioContext, out: AudioNode) => void> = {
  chime: (c, o) => { tone(c, o, 659.25, 0, 0.6); tone(c, o, 880, 0.18, 0.9); },
  bell: (c, o) => { for (const [f, p] of [[523.25, 1], [1046.5, 0.5], [1567.98, 0.25], [2093, 0.12]] as const) tone(c, o, f, 0, 1.6, "sine", p); },
  ping: (c, o) => tone(c, o, 1318.5, 0, 0.35, "triangle"),
  beep: (c, o) => { tone(c, o, 880, 0, 0.12, "square", 0.35); tone(c, o, 880, 0.2, 0.12, "square", 0.35); },
  marimba: (c, o) => { [523.25, 659.25, 783.99].forEach((f, k) => tone(c, o, f, k * 0.12, 0.3, "sine")); },
  alert: (c, o) => { [0, 0.25, 0.5].forEach((at) => { tone(c, o, 740, at, 0.1, "sawtooth", 0.3); tone(c, o, 988, at + 0.1, 0.1, "sawtooth", 0.3); }); },
};

/** Play a sound (`id` from SOUNDS) at `volume` 0–1. Returns false when nothing could be played. */
export function playSound(id: string = savedSound(), volume: number = savedVolume()): boolean {
  if (id === "off" || volume <= 0) return false;
  if (id === "custom") {
    const url = customSound();
    if (!url) return false;
    const a = new Audio(url);
    a.volume = Math.min(1, volume);
    void a.play().catch(() => undefined);
    return true;
  }
  const make = SYNTH[id];
  const c = audio();
  if (!make || !c) return false;
  const g = c.createGain();
  g.gain.value = Math.min(1, volume) * 0.5;
  g.connect(c.destination);
  make(c, g);
  return true;
}
