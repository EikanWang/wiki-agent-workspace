import { useEffect, useRef, useState } from "react";
import { FindEngine, FindOpts } from "../findText";
import { FindRequest } from "../search";

/**
 * Find in the shown document (Ctrl/⌘+F): every match highlighted, the current one scrolled to; Enter / Shift+Enter
 * (or the arrows) step through them. Ctrl/⌘+F again, while it has the focus, is the browser's own find.
 */
export default function FindBar({ engine, engineKey, request, focusKey, onClose }: {
  engine: () => FindEngine | null;
  /** changes when the engine (the view) or what it shows changes: look again */
  engineKey: string;
  /** a match to show (a link from the folder search) */
  request: (FindRequest & { at: number }) | null;
  /** bumped by Ctrl+F: focus the field (and select its text) */
  focusKey: number;
  onClose: () => void;
}) {
  const [q, setQ] = useState(request?.q ?? "");
  const [opts, setOpts] = useState<FindOpts>({ case: request?.case ?? false, word: request?.word ?? false });
  const [count, setCount] = useState<number | null>(null);
  const [cur, setCur] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const pending = useRef<(FindRequest & { at: number }) | null>(request);
  const curRef = useRef(0);
  curRef.current = cur;
  const lastKey = useRef("");

  useEffect(() => {
    if (!request) return;
    pending.current = request;
    setQ(request.q);
    setOpts({ case: request.case, word: request.word });
  }, [request]);

  useEffect(() => {
    input.current?.focus();
    input.current?.select();
  }, [focusKey]);

  // look the text up (as it is typed; again when the view changes)
  useEffect(() => {
    let alive = true;
    const t = window.setTimeout(async () => {
      const e = engine();
      if (!e) return setCount(null);
      if (!q.trim()) {
        e.clear();
        return setCount(null);
      }
      const n = await e.search(q, opts);
      if (!alive) return;
      setCount(n);
      const req = pending.current;
      const k = `${q}\0${opts.case}\0${opts.word}`;
      let scroll = k !== lastKey.current; // a new text: its first match; the same one looked up again: stay
      lastKey.current = k;
      let i = scroll ? 0 : Math.min(curRef.current, Math.max(0, n - 1));
      if (req && req.q === q) {
        pending.current = null;
        i = Math.max(0, e.pick(req));
        scroll = true;
      }
      setCur(i);
      if (n) e.show(i, scroll);
      else e.show(-1, false);
    }, 120);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, opts.case, opts.word, engineKey, request?.at]);

  useEffect(() => () => engine()?.clear(), [engine]);

  const step = (d: number) => {
    const e = engine();
    if (!e || !count) return;
    const i = (cur + d + count) % count;
    setCur(i);
    e.show(i, true);
  };

  return (
    <div className="find-bar" role="search" data-testid="find-bar">
      <input
        ref={input}
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setCur(0);
          pending.current = null;
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            step(e.shiftKey ? -1 : 1);
          } else if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            onClose();
          }
        }}
        placeholder="Find in document"
        aria-label="Find in document"
        spellCheck={false}
        data-testid="find-input"
      />
      <span className={`find-count ${q.trim() && count === 0 ? "none" : ""}`} data-testid="find-count">
        {!q.trim() || count === null ? "" : count === 0 ? "No results" : `${cur + 1} of ${count}${count >= 5000 ? "+" : ""}`}
      </span>
      <button className={`btn small toggle ${opts.case ? "on" : ""}`} aria-pressed={opts.case} onClick={() => setOpts((o) => ({ ...o, case: !o.case }))} title="Match case" data-testid="find-case">Aa</button>
      <button className={`btn small toggle ${opts.word ? "on" : ""}`} aria-pressed={opts.word} onClick={() => setOpts((o) => ({ ...o, word: !o.word }))} title="Whole words only" data-testid="find-word"><u>ab</u></button>
      <button className="icon-btn small" onClick={() => step(-1)} disabled={!count} title="Previous match (Shift+Enter)" aria-label="Previous match" data-testid="find-prev">↑</button>
      <button className="icon-btn small" onClick={() => step(1)} disabled={!count} title="Next match (Enter)" aria-label="Next match" data-testid="find-next">↓</button>
      <button className="icon-btn small" onClick={onClose} title="Close (Esc)" aria-label="Close find" data-testid="find-close">×</button>
    </div>
  );
}
