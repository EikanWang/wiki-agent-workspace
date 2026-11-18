import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type React from "react";

/** The views of the activity bar, in their default order. */
export const ACTIVITIES = ["files", "local", "reviews", "development", "agents", "terminal", "insights", "machines"] as const;
export type Activity = (typeof ACTIVITIES)[number];

/** `saved` (the user's order) made complete: unknown ids dropped, views it does not have yet added where they belong by default. */
export function orderOf(saved: readonly string[] | undefined): Activity[] {
  const known = (saved ?? []).filter((id, k, a): id is Activity => (ACTIVITIES as readonly string[]).includes(id) && a.indexOf(id) === k);
  const out = [...known];
  ACTIVITIES.forEach((id, k) => {
    if (out.includes(id)) return;
    const prev = ACTIVITIES.slice(0, k).reverse().find((p) => out.includes(p));
    out.splice(prev ? out.indexOf(prev) + 1 : 0, 0, id);
  });
  return out;
}

const isDefault = (order: Activity[]) => order.every((id, k) => id === ACTIVITIES[k]);

/**
 * The activity bar's buttons in the user's order: drag one to another place, or right-click one to move it up / down
 * or go back to the default order. `buttons` has the button of each view (null: not shown, e.g. no GitHub).
 */
export function ActivityButtons({ order, buttons, onOrder }: { order: Activity[]; buttons: Record<Activity, React.ReactNode>; onOrder: (o: Activity[]) => void }) {
  const [drag, setDrag] = useState<Activity | null>(null);
  const [over, setOver] = useState<{ id: Activity; after: boolean } | null>(null);
  const [menu, setMenu] = useState<{ id: Activity; x: number; y: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menu) return;
    const close = (e: Event) => { if (!(e.target instanceof Node && menuRef.current?.contains(e.target))) setMenu(null); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setMenu(null); };
    window.addEventListener("mousedown", close, true);
    window.addEventListener("keydown", key, true);
    return () => { window.removeEventListener("mousedown", close, true); window.removeEventListener("keydown", key, true); };
  }, [menu]);
  const shown = order.filter((id) => buttons[id]);
  /** `id` moved before / after `target` */
  const place = (id: Activity, target: Activity, after: boolean) => {
    if (id === target) return;
    const rest = order.filter((x) => x !== id);
    rest.splice(rest.indexOf(target) + (after ? 1 : 0), 0, id);
    onOrder(rest);
  };
  /** `id` one place up (-1) or down (+1) among the shown buttons */
  const step = (id: Activity, d: -1 | 1) => {
    const k = shown.indexOf(id);
    const other = shown[k + d];
    if (other) place(id, other, d > 0);
  };
  return (
    <>
      {shown.map((id) => (
        <div key={id} className={`activity-slot ${drag === id ? "dragging" : ""} ${over?.id === id && drag !== id ? (over.after ? "drop-after" : "drop-before") : ""}`}
          draggable data-activity={id} data-testid="activity-slot"
          onDragStart={(e) => { setDrag(id); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/x-waw-activity", id); }}
          onDragEnd={() => { setDrag(null); setOver(null); }}
          onDragOver={(e) => {
            if (!drag) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            const r = e.currentTarget.getBoundingClientRect();
            const after = e.clientY > r.top + r.height / 2;
            if (over?.id !== id || over.after !== after) setOver({ id, after });
          }}
          onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOver((o) => (o?.id === id ? null : o)); }}
          onDrop={(e) => {
            e.preventDefault();
            const from = (e.dataTransfer.getData("text/x-waw-activity") || drag) as Activity | null;
            const r = e.currentTarget.getBoundingClientRect();
            if (from) place(from, id, e.clientY > r.top + r.height / 2);
            setDrag(null);
            setOver(null);
          }}
          onContextMenu={(e) => { e.preventDefault(); setMenu({ id, x: e.clientX, y: e.clientY }); }}>
          {buttons[id]}
        </div>
      ))}
      {menu && createPortal(
        <div ref={menuRef} className="activity-menu" style={{ left: menu.x, top: Math.min(menu.y, window.innerHeight - 150) }} role="menu" data-testid="activity-menu">
          <div className="muted small">Drag the icons to reorder them</div>
          <button role="menuitem" disabled={shown.indexOf(menu.id) <= 0} onClick={() => { step(menu.id, -1); setMenu(null); }} data-testid="activity-move-up">Move up</button>
          <button role="menuitem" disabled={shown.indexOf(menu.id) >= shown.length - 1} onClick={() => { step(menu.id, 1); setMenu(null); }} data-testid="activity-move-down">Move down</button>
          <button role="menuitem" disabled={isDefault(order)} onClick={() => { onOrder([...ACTIVITIES]); setMenu(null); }} data-testid="activity-reset-order">Reset to the default order</button>
        </div>,
        document.body,
      )}
    </>
  );
}
