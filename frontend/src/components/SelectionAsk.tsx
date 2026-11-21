import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { COMPOSER_HINT, composerKeyDown } from "./keys";
import { PictureStrip, picturesOnly, usePictures } from "./Pictures";

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
export default function SelectionAsk({ container, onAsk, hint, testId = "sel-ask", within = ".msg.agent", label = "Ask about this", sendLabel = "Ask",
  pictures = true, optional = false, extra }: {
  container: React.RefObject<HTMLElement | null>;
  /** `images`: pictures pasted into the box (data: URLs) */
  onAsk: (part: PickedPart, question: string, images: string[]) => Promise<void>;
  hint: string;
  testId?: string;
  /** what a selection must be inside (an agent's answer by default) */
  within?: string;
  label?: string;
  sendLabel?: string;
  /** pictures can be pasted into the question */
  pictures?: boolean;
  /** the question may be left empty (a default one is asked) */
  optional?: boolean;
  /** more inside the open box, above its buttons (e.g. reference folders) */
  extra?: React.ReactNode;
}) {
  const [pick, setPick] = useState<(PickedPart & { x: number; y: number }) | null>(null);
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pics = usePictures();
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
      const msg = node?.closest<HTMLElement>(within);
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
  }, [container, within]);

  const close = () => {
    setPick(null);
    setOpen(false);
    setQ("");
    setErr(null);
    pics.clear();
  };
  const send = () => {
    const question = q.trim() || (pics.pictures.length ? picturesOnly(pics.pictures.length) : "");
    if (!pick || (!question && !optional) || busy) return;
    setBusy(true);
    setErr(null);
    onAsk(pick, question, pics.urls)
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
  const top = Math.max(8, Math.min(pick.y, window.innerHeight - (open ? (pics.pictures.length ? 300 : extra ? 330 : 230) : 40)));
  return (
    <div className={`sel-ask ${open ? "open" : ""}`} style={{ left, top, width: open ? w : undefined }} onMouseDown={(e) => !open && e.preventDefault()} data-testid={testId}>
      {!open ? (
        <button className="btn small primary" onClick={() => setOpen(true)} data-testid={`${testId}-open`}>{label}</button>
      ) : (
        <>
          <blockquote className="sel-ask-quote" data-testid={`${testId}-quote`}>{pick.quote.length > 300 ? `${pick.quote.slice(0, 300)}…` : pick.quote}</blockquote>
          <PictureStrip pics={pics} />
          <textarea autoFocus rows={3} value={q} onChange={(e) => setQ(e.target.value)} disabled={busy}
            placeholder={`Your question about this part${optional ? ", or leave it empty to have it explained" : ""} (${COMPOSER_HINT})`}
            {...(pictures ? pics.handlers : {})} onKeyDown={(e) => composerKeyDown(e, send, setQ, close)} data-testid={`${testId}-input`} />
          {extra}
          <div className="sel-ask-bar">
            <span className="muted small">{hint}</span>
            <span className="spacer" />
            <button className="btn small" onClick={close}>Cancel</button>
            <button className="btn small primary" disabled={busy || (!optional && !q.trim() && !pics.pictures.length)} onClick={send} data-testid={`${testId}-send`}>{busy ? "Asking…" : sendLabel}</button>
          </div>
          {err && <div className="error-text small" role="alert">{err}</div>}
        </>
      )}
    </div>
  );
}
