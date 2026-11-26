import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { writeClipboard } from "../clipboard";

/** Where a tab was right-clicked (the menu is open while set). */
export interface TabMenuAt { key: string; x: number; y: number }

/** Right-click state of a tab bar: `open(e, key)` in onContextMenu, render `<TabCloseMenu …>` once. */
export function useTabMenu() {
  const [at, setAt] = useState<TabMenuAt | null>(null);
  const open = (e: React.MouseEvent, key: string) => { e.preventDefault(); setAt({ key, x: e.clientX, y: e.clientY }); };
  return { at, open, done: () => setAt(null) };
}

/** The right-click menu of a tab, like the wiki reader's (and VS Code's): close it, the others, those to its left /
 * right, the saved ones, or all; copy its path. `onClose` gets the keys to close (it asks about unsaved changes). */
export default function TabCloseMenu({ at, keys, dirty, path, onClose, done, testId = "tab-menu" }: {
  at: TabMenuAt | null; keys: string[]; dirty?: (key: string) => boolean; path?: (key: string) => string;
  onClose: (keys: string[], keep: string | null) => void; done: () => void; testId?: string;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!at) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) done(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") done(); };
    window.addEventListener("mousedown", away, true);
    window.addEventListener("keydown", esc, true);
    window.addEventListener("blur", done);
    return () => { window.removeEventListener("mousedown", away, true); window.removeEventListener("keydown", esc, true); window.removeEventListener("blur", done); };
  }, [at, done]);
  if (!at) return null;
  const i = keys.indexOf(at.key);
  if (i < 0) return null;
  const run = (list: string[]) => { done(); if (list.length) onClose(list, list.includes(at.key) ? null : at.key); };
  const others = keys.filter((_, k) => k !== i);
  const saved = keys.filter((k) => !dirty?.(k));
  const items: [string, string, boolean, () => void][] = [
    ["Close", "close", true, () => run([at.key])],
    ["Close Others", "close-others", others.length > 0, () => run(others)],
    ["Close to the Left", "close-left", i > 0, () => run(keys.slice(0, i))],
    ["Close to the Right", "close-right", i < keys.length - 1, () => run(keys.slice(i + 1))],
    ...(dirty ? [["Close Saved", "close-saved", saved.length > 0, () => run(saved)] as [string, string, boolean, () => void]] : []),
    ["Close All", "close-all", true, () => run(keys)],
  ];
  return createPortal(
    <div ref={box} className="activity-menu" style={{ left: Math.min(at.x, window.innerWidth - 220), top: Math.min(at.y, window.innerHeight - 260) }}
      role="menu" data-testid={testId}>
      {items.map(([label, id, enabled, fn]) => (
        <button key={id} role="menuitem" disabled={!enabled} onClick={fn} data-testid={`${testId}-${id}`}>{label}</button>
      ))}
      {path && (
        <>
          <div className="ws-menu-sep" />
          <button role="menuitem" onClick={() => { done(); void writeClipboard(path(at.key)); }} data-testid={`${testId}-copy-path`}>Copy Path</button>
        </>
      )}
    </div>,
    document.body,
  );
}
