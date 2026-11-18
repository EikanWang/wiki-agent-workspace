import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { COMPOSER_HINT, composerKeyDown } from "./keys";

/** The part of an agent's answer the user selected, and where it is. */
export interface PickedPart {
  quote: string;
  messageId: string | null;
  threadId: string | null;
}

/** As the user's message: the selected part quoted, then the question. */
export function quotedQuestion(quote: string, question: string): string {
  return `${quote.split("\n").map((l) => `> ${l}`).join("\n")}\n\n${question}`;
}

/**
 * Select a part of an agent's answer inside `container` → "Ask about this": a question about just that part, typed in
 * a small box next to the selection (where the answer goes: `hint`).
 */
export default function SelectionAsk({ container, onAsk, hint, testId = "sel-ask" }: {
  container: React.RefObject<HTMLElement | null>;
  onAsk: (part: PickedPart, question: string) => Promise<void>;
  hint: string;
  testId?: string;
}) {
  const [pick, setPick] = useState<(PickedPart & { x: number; y: number }) | null>(null);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const openRef = useRef(false);
  openRef.current = open;

  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const onUp = () => window.setTimeout(() => {
      if (openRef.current) return;
      const sel = window.getSelection();
      const quote = sel && !sel.isCollapsed ? sel.toString().trim() : "";
      const node = sel?.anchorNode ? (sel.anchorNode.nodeType === 1 ? (sel.anchorNode as Element) : sel.anchorNode.parentElement) : null;
      const msg = node?.closest<HTMLElement>(".msg.agent");
      if (!quote || !msg || !el.contains(msg) || !sel?.rangeCount) return setPick(null);
      const r = sel.getRangeAt(0).getBoundingClientRect();
      setPick({ quote: quote.slice(0, 20000), messageId: msg.dataset.messageId ?? null, threadId: msg.closest<HTMLElement>("[data-thread-id]")?.dataset.threadId ?? null,
        x: r.left + r.width / 2, y: r.bottom + 6 });
    }, 10);
    const onScroll = () => { if (!openRef.current) setPick(null); };
    const onDown = (e: MouseEvent) => {
      if (!openRef.current && !(e.target as Element | null)?.closest?.(".sel-ask")) setPick(null);
    };
    el.addEventListener("mouseup", onUp);
    el.addEventListener("scroll", onScroll, true);
    document.addEventListener("mousedown", onDown);
    return () => {
      el.removeEventListener("mouseup", onUp);
      el.removeEventListener("scroll", onScroll, true);
      document.removeEventListener("mousedown", onDown);
    };
  }, [container]);

  const close = () => {
    setPick(null);
    setOpen(false);
    setQ("");
    setErr(null);
  };
  const send = () => {
    const question = q.trim();
    if (!pick || !question || busy) return;
    setBusy(true);
    setErr(null);
    onAsk(pick, question)
      .then(() => {
        close();
        window.getSelection()?.removeAllRanges();
      })
      .catch((e) => setErr((e as ApiError).message))
      .finally(() => setBusy(false));
  };

  if (!pick) return null;
  const w = open ? 360 : 130;
  const left = Math.max(8, Math.min(pick.x - w / 2, window.innerWidth - w - 8));
  const top = Math.max(8, Math.min(pick.y, window.innerHeight - (open ? 230 : 40)));
  return (
    <div className={`sel-ask ${open ? "open" : ""}`} style={{ left, top, width: open ? w : undefined }} onMouseDown={(e) => !open && e.preventDefault()} data-testid={testId}>
      {!open ? (
        <button className="btn small primary" onClick={() => setOpen(true)} data-testid={`${testId}-open`}>Ask about this</button>
      ) : (
        <>
          <blockquote className="sel-ask-quote" data-testid={`${testId}-quote`}>{pick.quote.length > 300 ? `${pick.quote.slice(0, 300)}…` : pick.quote}</blockquote>
          <textarea autoFocus rows={3} value={q} onChange={(e) => setQ(e.target.value)} disabled={busy} placeholder={`Your question about this part (${COMPOSER_HINT})`}
            onKeyDown={(e) => composerKeyDown(e, send, setQ, close)} data-testid={`${testId}-input`} />
          <div className="sel-ask-bar">
            <span className="muted small">{hint}</span>
            <span className="spacer" />
            <button className="btn small" onClick={close}>Cancel</button>
            <button className="btn small primary" disabled={busy || !q.trim()} onClick={send} data-testid={`${testId}-send`}>{busy ? "Asking…" : "Ask"}</button>
          </div>
          {err && <div className="error-text small" role="alert">{err}</div>}
        </>
      )}
    </div>
  );
}
