import { useCallback, useState } from "react";

/**
 * Resizable / foldable panes. `usePane` keeps a pane's size (px) and folded state per browser; `<Splitter>` is the
 * bar between two panes: drag to resize (pointer capture, so it keeps working over iframes and the editor),
 * double-click for the default size, and a small button to fold / unfold the pane.
 */
export interface Pane {
  size: number;
  folded: boolean;
  setSize: (n: number) => void;
  setFolded: (f: boolean) => void;
  reset: () => void;
}

export function usePane(key: string, def: number, min: number, max: number, defFolded = false): Pane {
  const read = (): { size: number; folded: boolean } => {
    try {
      const v = JSON.parse(localStorage.getItem(`waw.pane.${key}`) || "null");
      if (v && typeof v.size === "number") return { size: v.size, folded: !!v.folded };
    } catch {
      /* ignore */
    }
    return { size: def, folded: defFolded };
  };
  const [st, setSt] = useState(read);
  const save = useCallback((next: { size: number; folded: boolean }) => {
    setSt(next);
    localStorage.setItem(`waw.pane.${key}`, JSON.stringify(next));
  }, [key]);
  return {
    size: Math.max(min, Math.min(max, st.size)),
    folded: st.folded,
    setSize: (n) => save({ size: Math.round(Math.max(min, Math.min(max, n))), folded: false }),
    setFolded: (f) => save({ ...st, folded: f }),
    reset: () => save({ size: def, folded: false }),
  };
}

/**
 * dir "x": a vertical bar between left / right panes (resizes width); "y": a horizontal bar (resizes height).
 * `side`: which pane it sizes — "before" (left / top) or "after" (right / bottom); dragging toward the other pane
 * makes it bigger.
 */
export function Splitter({ dir, side, pane, label, testId, className = "", style }: { dir: "x" | "y"; side: "before" | "after"; pane: Pane; label: string; testId?: string; className?: string; style?: React.CSSProperties }) {
  const sign = side === "before" ? 1 : -1;
  const arrow = dir === "x"
    ? (pane.folded === (side === "before") ? "›" : "‹")
    : (pane.folded === (side === "before") ? "▾" : "▴");
  return (
    <div className={`splitter ${dir} ${pane.folded ? "folded" : ""} ${className}`} style={style} role="separator" aria-orientation={dir === "x" ? "vertical" : "horizontal"}
      title={`Drag to resize the ${label} · double-click: default size`} data-testid={testId}
      onDoubleClick={(e) => { if ((e.target as HTMLElement).closest(".splitter-fold")) return; pane.reset(); }}
      onPointerDown={(e) => {
        if (e.button !== 0 || (e.target as HTMLElement).closest(".splitter-fold")) return;
        const el = e.currentTarget;
        const start = dir === "x" ? e.clientX : e.clientY;
        const startSize = pane.folded ? 0 : pane.size;
        el.setPointerCapture(e.pointerId);
        document.body.classList.add(dir === "x" ? "resizing-h" : "resizing-v");
        const move = (ev: PointerEvent) => pane.setSize(startSize + sign * ((dir === "x" ? ev.clientX : ev.clientY) - start));
        const up = (ev: PointerEvent) => {
          el.releasePointerCapture(ev.pointerId);
          el.removeEventListener("pointermove", move);
          el.removeEventListener("pointerup", up);
          el.removeEventListener("pointercancel", up);
          document.body.classList.remove("resizing-h", "resizing-v");
        };
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", up);
        el.addEventListener("pointercancel", up);
        e.preventDefault();
      }}>
      <button className="splitter-fold" onClick={() => pane.setFolded(!pane.folded)} title={pane.folded ? `Show the ${label}` : `Fold the ${label}`}
        aria-label={pane.folded ? `Show the ${label}` : `Fold the ${label}`} data-testid={testId ? `${testId}-fold` : undefined}>{arrow}</button>
    </div>
  );
}
