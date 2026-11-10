import { KeyboardEvent, TextareaHTMLAttributes, useEffect, useRef, useState } from "react";
import { get } from "../api";

export interface MentionCandidate { login: string; name: string | null; source: string }

/** The `@word` being typed right before the caret, if any. */
function mentionAt(value: string, caret: number): { start: number; q: string } | null {
  const before = value.slice(0, caret);
  const m = before.match(/(^|[^\w`])@([\w.-]{0,39})$/);
  return m ? { start: caret - m[2].length - 1, q: m[2] } : null;
}

const SOURCE: Record<string, string> = { author: "author", reviewer: "reviewer", requested: "review requested", contributor: "contributor", commenter: "commented", repository: "" };

/**
 * A textarea with GitHub-style @-mention suggestions: typing `@` lists the PR's participants first (author,
 * reviewers, requested reviewers, contributors, commenters), then users of the repository matching what is typed.
 * ↑/↓ choose, Enter or Tab inserts, Esc closes the list; otherwise keys go to the box's own handler (send etc.).
 */
export default function MentionTextarea({ apiBase, value, onValue, onKeyDown, ...rest }: Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange"> & {
  /** /github/pr/{owner}/{repo}/{n} */
  apiBase: string;
  value: string;
  onValue: (v: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [at, setAt] = useState<{ start: number; q: string } | null>(null);
  const [items, setItems] = useState<MentionCandidate[]>([]);
  const [sel, setSel] = useState(0);
  const seq = useRef(0);

  useEffect(() => {
    if (!at) {
      setItems([]);
      return;
    }
    const n = ++seq.current;
    const t = window.setTimeout(() => {
      get<{ candidates: MentionCandidate[] }>(`${apiBase}/mentions?q=${encodeURIComponent(at.q)}`)
        .then((r) => { if (n === seq.current) { setItems(r.candidates); setSel(0); } })
        .catch(() => undefined);
    }, at.q ? 150 : 0);
    return () => window.clearTimeout(t);
  }, [at?.q, at?.start, apiBase]); // eslint-disable-line react-hooks/exhaustive-deps

  const track = (v: string, caret: number) => setAt(mentionAt(v, caret));
  const insert = (c: MentionCandidate) => {
    if (!at) return;
    const ta = ref.current;
    const caret = ta ? ta.selectionStart : value.length;
    const next = value.slice(0, at.start) + `@${c.login} ` + value.slice(caret);
    onValue(next);
    setAt(null);
    const pos = at.start + c.login.length + 2;
    window.setTimeout(() => { if (ta) { ta.focus(); ta.selectionStart = ta.selectionEnd = pos; } }, 0);
  };
  const open = !!at && items.length > 0;
  const keyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setSel((i) => (i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length);
        return;
      }
      if ((e.key === "Enter" || e.key === "Tab") && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        insert(items[sel]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setAt(null);
        return;
      }
    }
    onKeyDown?.(e);
  };
  return (
    <span className="mention-wrap">
      <textarea ref={ref} {...rest} value={value}
        onChange={(e) => { onValue(e.target.value); track(e.target.value, e.target.selectionStart); }}
        onKeyDown={keyDown}
        onKeyUp={(e) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) track(e.currentTarget.value, e.currentTarget.selectionStart); }}
        onClick={(e) => track(e.currentTarget.value, e.currentTarget.selectionStart)}
        onBlur={() => window.setTimeout(() => setAt(null), 150)} />
      {open && (
        <ul className="mention-list" role="listbox" data-testid="mention-list">
          {items.map((c, i) => (
            <li key={c.login} role="option" aria-selected={i === sel} className={i === sel ? "on" : ""} data-testid="mention-item"
              onMouseDown={(e) => { e.preventDefault(); insert(c); }} onMouseEnter={() => setSel(i)}>
              <b>{c.login}</b>{c.name ? <span className="muted"> {c.name}</span> : null}{SOURCE[c.source] ? <span className="muted small"> · {SOURCE[c.source]}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </span>
  );
}
