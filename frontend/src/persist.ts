import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Small UI state that survives a reload or closing the browser (localStorage): which sub-tab is shown, where a
 * long view was scrolled, text typed but not sent yet. Keys start with `waw.keep.`; old entries are pruned.
 */
const PREFIX = "waw.keep.";
const MAX_KEYS = 300;

function read<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw === null ? fallback : (JSON.parse(raw) as { v: T }).v;
  } catch {
    return fallback;
  }
}

function write(key: string, value: unknown): void {
  try {
    if (value === "" || value === null || value === undefined) localStorage.removeItem(PREFIX + key);
    else localStorage.setItem(PREFIX + key, JSON.stringify({ v: value, t: Date.now() }));
  } catch {
    prune(true);
  }
}

/** Keep at most MAX_KEYS entries (oldest dropped); on a full storage drop half. */
function prune(hard = false): void {
  const entries: [string, number][] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (!k || !k.startsWith(PREFIX)) continue;
    try {
      entries.push([k, (JSON.parse(localStorage.getItem(k) || "{}") as { t?: number }).t ?? 0]);
    } catch {
      entries.push([k, 0]);
    }
  }
  const limit = hard ? Math.floor(entries.length / 2) : MAX_KEYS;
  if (entries.length <= limit) return;
  entries.sort((a, b) => a[1] - b[1]);
  for (const [k] of entries.slice(0, entries.length - limit)) localStorage.removeItem(k);
}
let pruned = false;

/** useState that is remembered under `key` (re-read when the key changes). */
export function usePersistentState<T>(key: string, initial: T): [T, (v: T | ((prev: T) => T)) => void] {
  if (!pruned) {
    pruned = true;
    prune();
  }
  const [state, setState] = useState<T>(() => read(key, initial));
  const keyRef = useRef(key);
  useEffect(() => {
    if (keyRef.current !== key) {
      keyRef.current = key;
      setState(read(key, initial));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const set = useCallback((v: T | ((prev: T) => T)) => {
    setState((prev) => {
      const next = typeof v === "function" ? (v as (p: T) => T)(prev) : v;
      write(keyRef.current, next);
      return next;
    });
  }, []);
  return [state, set];
}

/**
 * Remember where a scrolling element was (under `key`) and scroll back there once its content is `ready` —
 * retrying while the content is still growing (diffs and comments load in steps).
 */
export function useRestoredScroll(key: string, ready: boolean) {
  const el = useRef<HTMLElement | null>(null);
  const restored = useRef<string | null>(null);
  const ref = useCallback((node: HTMLElement | null) => {
    el.current = node;
  }, []);
  useEffect(() => {
    const node = el.current;
    if (!node) return;
    let timer: number | undefined;
    const save = () => write(`scroll.${key}`, Math.round(node.scrollTop));
    const onScroll = () => {
      if (restored.current !== key) return; // not before the saved position was applied
      window.clearTimeout(timer);
      timer = window.setTimeout(save, 250);
    };
    node.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("pagehide", save);
    return () => {
      window.clearTimeout(timer);
      node.removeEventListener("scroll", onScroll);
      window.removeEventListener("pagehide", save);
    };
  }, [key, ready]);
  useEffect(() => {
    const node = el.current;
    if (!node || !ready || restored.current === key) return;
    const want = read<number>(`scroll.${key}`, 0);
    if (!want) {
      restored.current = key;
      return;
    }
    let tries = 0;
    let frame = 0;
    const stop = () => { restored.current = key; window.clearTimeout(frame); }; // the user scrolls: their choice wins
    node.addEventListener("wheel", stop, { once: true, passive: true });
    node.addEventListener("touchstart", stop, { once: true, passive: true });
    node.addEventListener("keydown", stop, { once: true });
    const step = () => {
      if (restored.current === key) return;
      node.scrollTop = want;
      if (Math.abs(node.scrollTop - want) <= 2 || ++tries > 120) {
        restored.current = key;
        return;
      }
      frame = window.setTimeout(step, 50); // content still loading: try again
    };
    step();
    return () => {
      window.clearTimeout(frame);
      node.removeEventListener("wheel", stop);
      node.removeEventListener("touchstart", stop);
      node.removeEventListener("keydown", stop);
    };
  }, [key, ready]);
  return ref;
}
