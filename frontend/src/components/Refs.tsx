import { createContext, useContext, useEffect, useRef, useState } from "react";
import { renderMarkdown } from "../markdown";

/** Where a link of an agent answer can go (POST /refs/resolve). */
export interface RefCandidate {
  kind: "doc" | "local";
  workspace_id?: string;
  workspace_name?: string;
  path?: string;
  abs: string;
  fragment: string;
}

export interface RefItem {
  href: string;
  label: string;
}

/** Follows a link of an agent answer (null: links behave as plain links, e.g. outside the app's main window). */
export const RefContext = createContext<((href: string) => void) | null>(null);
export const useRefOpen = () => useContext(RefContext);

const PATH_IN_CODE = /^\/[^\s`<>"]+\.[A-Za-z0-9]{1,8}(?:#[^\s`<>"]*)?$/;

/** Inline code that is an absolute file path (`/a/b.md#Section`) becomes a link like the agent's own links. */
export function linkifyPaths(html: string): string {
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  tpl.content.querySelectorAll("code").forEach((c) => {
    if (c.closest("pre, a")) return;
    const t = (c.textContent || "").trim();
    if (!PATH_IN_CODE.test(t)) return;
    const a = document.createElement("a");
    a.setAttribute("href", t);
    a.className = "ref-path";
    c.replaceWith(a);
    a.appendChild(c);
  });
  return tpl.innerHTML;
}

/** Is this link one to follow in the app (a file, not a web page or an anchor of the answer itself)? */
export function isRefHref(href: string | null): href is string {
  return !!href && !href.startsWith("#") && !/^(https?|mailto):/i.test(href);
}

/** The links of an answer to files, in order, each once. */
export function refsIn(text: string): RefItem[] {
  const tpl = document.createElement("template");
  tpl.innerHTML = linkifyPaths(renderMarkdown(text, ""));
  const out: RefItem[] = [];
  tpl.content.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href");
    if (!isRefHref(href) || out.some((r) => r.href === href)) return;
    out.push({ href, label: (a.textContent || "").trim() || href });
  });
  return out;
}

/** The place a link names, for display: `file.md › Section`, `paper.pdf › page 7`. */
export function placeOf(href: string): string {
  const [p, frag = ""] = href.split("#");
  let name = p.split("/").filter(Boolean).pop() ?? p;
  let f = frag;
  try {
    name = decodeURIComponent(name);
    f = decodeURIComponent(frag);
  } catch {
    /* keep as written */
  }
  const page = /^(page|slide)=(\d+)$/i.exec(f);
  const lines = /^L(\d+)(?:-L?(\d+))?$/.exec(f);
  const where = page ? `${page[1].toLowerCase()} ${page[2]}` : lines ? `lines ${lines[1]}${lines[2] ? `–${lines[2]}` : ""}` : f;
  return where ? `${name} › ${where}` : name;
}

/** "References (n)": every file place the answer links, to jump to one. */
export function RefsMenu({ text }: { text: string }) {
  const open = useRefOpen();
  const [show, setShow] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!show) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setShow(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [show]);
  if (!open) return null;
  const refs = refsIn(text);
  if (refs.length < 2) return null;
  return (
    <div className="refs-menu" ref={box}>
      <button className="btn small" onClick={() => setShow((v) => !v)} aria-expanded={show} data-testid="refs-menu"
        title="The places this answer refers to: choose one to open it">
        References ({refs.length}) ▾
      </button>
      {show && (
        <ul className="refs-list" role="menu" data-testid="refs-list">
          {refs.map((r) => (
            <li key={r.href}>
              <button role="menuitem" onClick={() => { setShow(false); open(r.href); }} title={r.href} data-testid="refs-item">
                <span className="ref-label">{r.label}</span>
                <span className="muted small">{placeOf(r.href)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** A link that matches several files: choose which to open. */
export function RefPicker({ target, candidates, onPick, onClose }: { target: string; candidates: RefCandidate[]; onPick: (c: RefCandidate) => void; onClose: () => void }) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-label="Choose the file to open" onClick={(e) => e.stopPropagation()} data-testid="ref-picker">
        <div className="modal-head">
          <span>{candidates.length} files match <code>{placeOf(target)}</code></span>
          <button className="icon-btn small" onClick={onClose} aria-label="Close">×</button>
        </div>
        <div className="muted small">The answer links <code>{target.split("#")[0]}</code>; choose the one to open.</div>
        <ul className="refs-list static">
          {candidates.map((c, i) => (
            <li key={`${c.workspace_id ?? ""}:${c.abs}`}>
              <button onClick={() => onPick(c)} autoFocus={i === 0} data-testid="ref-candidate">
                <span className="ref-label">{c.path ?? c.abs}</span>
                <span className="muted small">{c.kind === "doc" ? c.workspace_name : "Local files"} · {c.abs}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
