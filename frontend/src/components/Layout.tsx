import { useEffect, useRef } from "react";

/** Vertical drag handle for resizing an adjacent panel. Calls onResize with the pointer's x delta. */
export function Resizer({ onResize, onEnd, side }: { onResize: (dx: number) => void; onEnd?: () => void; side: "left" | "right" }) {
  const dragging = useRef(false);
  const lastX = useRef(0);
  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (!dragging.current) return;
      const dx = e.clientX - lastX.current;
      lastX.current = e.clientX;
      onResize(dx);
    };
    const up = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.classList.remove("resizing");
      onEnd?.();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
    };
  }, [onResize, onEnd]);
  return (
    <div
      className={`resizer ${side}`}
      role="separator"
      aria-orientation="vertical"
      title="Drag to resize"
      onPointerDown={(e) => {
        dragging.current = true;
        lastX.current = e.clientX;
        document.body.classList.add("resizing");
        e.preventDefault();
      }}
    />
  );
}

// ---------------------------------------------------------------- persisted preferences

export interface Prefs {
  readerFont: "system" | "serif" | "mono";
  readerSize: number; // px
  readerWidth: number; // max content width px; 0 = fill the available width
  terminalSize: number; // px
  sidebarWidth: number;
  panelWidth: number;
  sidebarOpen: boolean;
  /** the sidebar's list (folders, projects, PRs…) next to the activity bar; folded: only the activity bar shows */
  listOpen: boolean;
  /** what the sidebar shows (activity bar): the file tree or the GitHub review queue */
  sidebarView: "files" | "reviews" | "development" | "local" | "agents" | "terminal" | "insights" | "machines";
  panelOpen: boolean;
  tocOpen: boolean;
  /** the middle column (document / PR) is folded and the discussion panel takes its width */
  centerFolded?: boolean;
  /** Discussion panel: one discussion at a time, or all discussions of the document as one stream. */
  panelMode: "individual" | "flat";
  /** Reader interaction: "read" = plain viewer (selection only copies), "discuss" = selection opens the question box. */
  readerMode: "read" | "discuss";
  /** Word / PowerPoint / Visio: the original layout (LibreOffice) or the text view (anchored discussions) */
  officeView: "layout" | "text";
  /** the rectangle around the place a link led to: its color, and how long it shows (ms; 0 = none) */
  linkHighlightColor: string;
  linkHighlightMs: number;
  /** the activity bar's views in the user's order (see ActivityOrder) */
  activityOrder?: string[];
}

export const DEFAULT_PREFS: Prefs = {
  readerFont: "system",
  readerSize: 15,
  readerWidth: 0,
  terminalSize: 13,
  sidebarWidth: 260,
  panelWidth: 380,
  sidebarOpen: true,
  listOpen: true,
  sidebarView: "files",
  panelOpen: true,
  tocOpen: true,
  panelMode: "individual",
  readerMode: "discuss",
  officeView: "layout",
  linkHighlightColor: "#f5a623",
  linkHighlightMs: 1000,
};

const KEY = "waw.prefs";

export function loadPrefs(): Prefs {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULT_PREFS };
    const stored = JSON.parse(raw) as Partial<Prefs>;
    if (stored.readerWidth === 820) stored.readerWidth = 0; // migrate the old fixed default to "fill"
    return { ...DEFAULT_PREFS, ...stored };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function savePrefs(p: Prefs) {
  try {
    localStorage.setItem(KEY, JSON.stringify(p));
  } catch {
    /* ignore quota errors */
  }
}

export const FONT_STACKS: Record<Prefs["readerFont"], string> = {
  system: 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif',
  serif: 'Georgia, "Times New Roman", "Noto Serif", "Songti SC", serif',
  mono: 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
};

export function FontSettings({ prefs, onChange, onClose }: { prefs: Prefs; onChange: (p: Partial<Prefs>) => void; onClose: () => void }) {
  return (
    <div className="popover-menu" data-testid="font-settings" onMouseDown={(e) => e.stopPropagation()}>
      <div className="sel-head">
        <span>Appearance</span>
        <button className="icon-btn" onClick={onClose} aria-label="Close">×</button>
      </div>
      <label className="pref-row">
        <span>Document font</span>
        <select value={prefs.readerFont} onChange={(e) => onChange({ readerFont: e.target.value as Prefs["readerFont"] })} data-testid="pref-font">
          <option value="system">Sans-serif (system)</option>
          <option value="serif">Serif</option>
          <option value="mono">Monospace</option>
        </select>
      </label>
      <label className="pref-row">
        <span>Document size</span>
        <span className="stepper">
          <button className="btn small" onClick={() => onChange({ readerSize: Math.max(11, prefs.readerSize - 1) })} aria-label="Smaller">−</button>
          <input type="number" min={11} max={28} value={prefs.readerSize} onChange={(e) => onChange({ readerSize: Math.min(28, Math.max(11, Number(e.target.value) || 15)) })} data-testid="pref-size" />
          <span className="muted small">px</span>
          <button className="btn small" onClick={() => onChange({ readerSize: Math.min(28, prefs.readerSize + 1) })} aria-label="Larger">+</button>
        </span>
      </label>
      <label className="pref-row">
        <span>Content width</span>
        <span className="stepper">
          <label className="small"><input type="checkbox" checked={prefs.readerWidth === 0} onChange={(e) => onChange({ readerWidth: e.target.checked ? 0 : 820 })} data-testid="pref-fill" /> fill</label>
          <input type="range" min={560} max={1600} step={20} value={prefs.readerWidth || 1600} disabled={prefs.readerWidth === 0} onChange={(e) => onChange({ readerWidth: Number(e.target.value) })} />
          <span className="muted small">{prefs.readerWidth ? `${prefs.readerWidth}px` : "fill"}</span>
        </span>
      </label>
      <label className="pref-row">
        <span>Terminal size</span>
        <span className="stepper">
          <button className="btn small" onClick={() => onChange({ terminalSize: Math.max(9, prefs.terminalSize - 1) })} aria-label="Smaller">−</button>
          <input type="number" min={9} max={24} value={prefs.terminalSize} onChange={(e) => onChange({ terminalSize: Math.min(24, Math.max(9, Number(e.target.value) || 13)) })} />
          <span className="muted small">px</span>
          <button className="btn small" onClick={() => onChange({ terminalSize: Math.min(24, prefs.terminalSize + 1) })} aria-label="Larger">+</button>
        </span>
      </label>
      <label className="pref-row" title="When a link opens a document, a rectangle shows the place it points at for a moment">
        <span>Link highlight</span>
        <span className="stepper">
          <input type="color" value={prefs.linkHighlightColor} onChange={(e) => onChange({ linkHighlightColor: e.target.value })} aria-label="Highlight color" data-testid="pref-link-hl-color" />
          <input type="number" min={0} max={10} step={0.5} value={prefs.linkHighlightMs / 1000} onChange={(e) => onChange({ linkHighlightMs: Math.round(Math.min(10, Math.max(0, Number(e.target.value) || 0)) * 1000) })} aria-label="Highlight time (seconds, 0: off)" data-testid="pref-link-hl-time" />
          <span className="muted small">s</span>
        </span>
      </label>
      <div className="row">
        <button className="btn small" onClick={() => onChange({ readerFont: DEFAULT_PREFS.readerFont, readerSize: DEFAULT_PREFS.readerSize, readerWidth: DEFAULT_PREFS.readerWidth, terminalSize: DEFAULT_PREFS.terminalSize, linkHighlightColor: DEFAULT_PREFS.linkHighlightColor, linkHighlightMs: DEFAULT_PREFS.linkHighlightMs })}>Reset</button>
      </div>
    </div>
  );
}
