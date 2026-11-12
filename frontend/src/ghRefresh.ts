import { createElement, useEffect, useState, useSyncExternalStore } from "react";

/**
 * How often the app refreshes from GitHub on its own, per browser: the Reviews list (and its badge), and an open pull
 * request (a PR tab, PR Development's Pull request tab). 0 = never (only ↻). Refreshes run only while the page is
 * visible; coming back to the page after the interval refreshes at once.
 */
export type GhRefreshKind = "reviews" | "pr";
export const GH_REFRESH_DEFAULTS: Record<GhRefreshKind, number> = { reviews: 5, pr: 0 };
export const GH_REFRESH_CHOICES = [0, 1, 2, 5, 10, 15, 30, 60, 120, 240, 480];

const KEY = (k: GhRefreshKind) => `waw.ghRefresh.${k}`;
const subs = new Set<() => void>();

export function ghRefreshMinutes(kind: GhRefreshKind): number {
  const v = Number(localStorage.getItem(KEY(kind)));
  return localStorage.getItem(KEY(kind)) !== null && Number.isFinite(v) && v >= 0 ? v : GH_REFRESH_DEFAULTS[kind];
}

export function setGhRefreshMinutes(kind: GhRefreshKind, minutes: number) {
  localStorage.setItem(KEY(kind), String(minutes));
  subs.forEach((f) => f());
}

export function useGhRefreshMinutes(kind: GhRefreshKind): number {
  return useSyncExternalStore((cb) => { subs.add(cb); const onStorage = (e: StorageEvent) => { if (e.key === KEY(kind)) cb(); };
    window.addEventListener("storage", onStorage); return () => { subs.delete(cb); window.removeEventListener("storage", onStorage); }; },
  () => ghRefreshMinutes(kind));
}

/** Call `refresh` every `kind` interval while the page is visible (and at once when it comes back overdue). */
export function useGhAutoRefresh(kind: GhRefreshKind, refresh: () => void, enabled = true) {
  const minutes = useGhRefreshMinutes(kind);
  const [last, setLast] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled || minutes <= 0) return;
    const ms = minutes * 60_000;
    let at = last;
    const tick = () => {
      if (document.visibilityState !== "visible" || Date.now() - at < ms - 500) return;
      at = Date.now();
      setLast(at);
      refresh();
    };
    const t = window.setInterval(tick, Math.min(ms, 30_000));
    document.addEventListener("visibilitychange", tick);
    return () => { window.clearInterval(t); document.removeEventListener("visibilitychange", tick); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [minutes, enabled, refresh]);
}

const label = (m: number) => (m === 0 ? "off (↻ only)" : m < 60 ? `${m} min` : `${m / 60} h`);

/** "auto-refresh [5 min ▾]" */
export function GhRefreshSelect({ kind, testId, title }: { kind: GhRefreshKind; testId?: string; title?: string }) {
  const minutes = useGhRefreshMinutes(kind);
  const choices = GH_REFRESH_CHOICES.includes(minutes) ? GH_REFRESH_CHOICES : [...GH_REFRESH_CHOICES, minutes].sort((a, b) => a - b);
  return createElement("label", { className: "gh-refresh small muted", title: title ?? "How often to refresh from GitHub on its own (while this page is visible); ↻ refreshes now" },
    "auto-refresh ",
    createElement("select", { value: minutes, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => setGhRefreshMinutes(kind, Number(e.target.value)), "data-testid": testId },
      ...choices.map((m) => createElement("option", { key: m, value: m }, label(m)))));
}
