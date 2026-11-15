import { useEffect, useRef, useState } from "react";
import { writeClipboard } from "../clipboard";

/** Copy a pull request's / issue's link to share it: the link (one click), or from ▾ the link with its title, or a
 * Markdown link. */
export default function CopyLink({ url, title, label, testId = "copy-link" }: { url: string; title: string; label: string; testId?: string }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!open) return;
    const off = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", off);
    return () => document.removeEventListener("mousedown", off);
  }, [open]);
  const copy = async (text: string, what: string) => {
    setOpen(false);
    setDone((await writeClipboard(text)) ? `Copied ${what}` : "Copy failed: select the link on GitHub instead");
    window.setTimeout(() => setDone(null), 2500);
  };
  return (
    <span className="ws-menu-anchor copy-link" ref={ref}>
      <span className="btn-group">
        <button className="btn small" onClick={() => void copy(url, "the link")} title={`Copy the link: ${url}`} data-testid={testId}>🔗 Copy link</button>
        <button className="btn small" onClick={() => setOpen((v) => !v)} aria-expanded={open} aria-label="More ways to copy" data-testid={`${testId}-more`}>▾</button>
      </span>
      {open && (
        <span className="ws-menu copy-link-menu" data-testid={`${testId}-menu`}>
          <button className="list-item" onClick={() => void copy(url, "the link")} data-testid={`${testId}-url`}>Link<span className="muted small mono"> {url}</span></button>
          <button className="list-item" onClick={() => void copy(`${label}: ${title}\n${url}`, "the title and link")} data-testid={`${testId}-titled`}>Title and link</button>
          <button className="list-item" onClick={() => void copy(`[${label}: ${title.replace(/[[\]]/g, "\\$&")}](${url})`, "a Markdown link")} data-testid={`${testId}-md`}>Markdown link</button>
        </span>
      )}
      {done && <span className={`muted small copy-link-done`} role="status" data-testid={`${testId}-done`}>{done}</span>}
    </span>
  );
}
