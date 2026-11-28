import { useCallback, useEffect, useState } from "react";
import { get } from "./api";

/** What the Repo Insights badges count: the unread new things of the daily summaries, of the reports, or both. */
export type BadgeMode = "daily+reports" | "daily" | "reports" | "off";
export type Unread = Record<string, { daily: number; reports: number }>;

const KEY = "waw.insights.badge";
const EVENT = "waw-insights-unread";

export function badgeMode(): BadgeMode {
  const v = localStorage.getItem(KEY);
  return v === "daily" || v === "reports" || v === "off" ? v : "daily+reports";
}

export function setBadgeMode(m: BadgeMode) {
  localStorage.setItem(KEY, m);
  window.dispatchEvent(new Event(EVENT));
}

/** Something was marked read / unread (or a report / summary was made): the badges count again. */
export function unreadChanged() {
  window.dispatchEvent(new Event(EVENT));
}

export function countOf(u: { daily: number; reports: number } | undefined, mode: BadgeMode): number {
  if (!u || mode === "off") return 0;
  return (mode !== "reports" ? u.daily : 0) + (mode !== "daily" ? u.reports : 0);
}

/** The unread counts of every repository, refreshed every minute and when something is marked read. */
export function useInsightsUnread(enabled: boolean) {
  const [counts, setCounts] = useState<Unread>({});
  const [mode, setMode] = useState<BadgeMode>(badgeMode);
  const load = useCallback(async () => {
    try {
      setCounts((await get<{ repos: Unread }>("/insights/unread")).repos);
    } catch {
      /* keep the last counts */
    }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    void load();
    const t = setInterval(() => void load(), 60_000);
    const on = () => { setMode(badgeMode()); void load(); };
    window.addEventListener(EVENT, on);
    return () => { clearInterval(t); window.removeEventListener(EVENT, on); };
  }, [enabled, load]);
  const total = Object.values(counts).reduce((s, u) => s + countOf(u, mode), 0);
  return { counts, mode, total };
}
