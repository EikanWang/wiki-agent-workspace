import { useEffect, useRef, useState } from "react";

/** Defer a PR review: it leaves the list until a time you pick and/or until someone does something on the PR. */
export interface Deferral { until: string | null; on_activity: boolean; at: string; note: string }

const at9 = (d: Date) => { const x = new Date(d); x.setHours(9, 0, 0, 0); return x; };
function choices(): [string, Date][] {
  const now = new Date();
  const tomorrow = at9(new Date(now.getTime() + 86400_000));
  const monday = at9(new Date(now.getTime() + (((8 - now.getDay()) % 7) || 7) * 86400_000));
  return [["In 3 hours", new Date(now.getTime() + 3 * 3600_000)], ["Tomorrow 09:00", tomorrow], ["Next Monday 09:00", monday],
    ["In a week", at9(new Date(now.getTime() + 7 * 86400_000))], ["In a month", at9(new Date(now.getTime() + 30 * 86400_000))]];
}
const local = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);

export function deferLabel(d: Deferral): string {
  const t = d.until ? new Date(d.until).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
  return d.until && d.on_activity ? `until ${t} or new activity` : d.until ? `until ${t}` : "until new activity";
}

export default function DeferMenu({ pr, onDefer }: { pr: string; onDefer: (d: { pr: string; until: string | null; on_activity: boolean; note: string }) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [activity, setActivity] = useState(true);
  const [custom, setCustom] = useState(() => local(at9(new Date(Date.now() + 2 * 86400_000))));
  const [note, setNote] = useState("");
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", off);
    return () => document.removeEventListener("mousedown", off);
  }, [open]);
  const go = async (until: Date | null, onActivity: boolean) => {
    setBusy(true);
    try {
      await onDefer({ pr, until: until ? until.toISOString() : null, on_activity: onActivity, note });
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="level-menu defer-menu" ref={ref} onClick={(e) => e.stopPropagation()}>
      <span role="button" tabIndex={0} className="icon-btn small defer-btn" title="Defer this review (it leaves the list for a while)" aria-label="Defer review"
        onClick={() => setOpen((v) => !v)} onKeyDown={(e) => { if (e.key === "Enter") setOpen((v) => !v); }} data-testid="review-defer">⏰</span>
      {open && (
        <span className="level-pop defer-pop" data-testid="defer-pop">
          <span className="muted small">Defer the review of {pr}</span>
          {choices().map(([label, d]) => (
            <button key={label} className="level-opt" disabled={busy} onClick={() => void go(d, activity)} data-testid="defer-choice" data-choice={label}>{label}</button>
          ))}
          <span className="defer-custom">
            <input type="datetime-local" value={custom} min={local(new Date())} onChange={(e) => setCustom(e.target.value)} data-testid="defer-date" />
            <button className="btn small" disabled={busy || !custom} onClick={() => void go(new Date(custom), activity)} data-testid="defer-date-ok">Defer</button>
          </span>
          <label className="small"><input type="checkbox" checked={activity} onChange={(e) => setActivity(e.target.checked)} data-testid="defer-activity" /> back earlier when there is new activity</label>
          <button className="level-opt" disabled={busy} onClick={() => void go(null, true)} data-testid="defer-until-activity">Until there is new activity</button>
          <input className="defer-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="note (optional), e.g. waiting for the RFC" maxLength={300} data-testid="defer-note" />
        </span>
      )}
    </span>
  );
}
