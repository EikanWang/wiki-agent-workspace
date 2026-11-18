import { useEffect, useState } from "react";
import { writeClipboard } from "./clipboard";

/** Copying the lines selected in a diff (a drag over diff lines selects lines, not text): Ctrl/⌘+C or a Copy button.
 * The diff whose selection was made last is the one copied. */

export interface CopyLine {
  kind: string;
  text: string;
}

/** The code of the selected lines as shown: `side` "left" leaves out added lines, "right" deleted ones (side by side). */
export function linesText(lines: CopyLine[], side: "left" | "right" | null = null): string {
  return lines
    .filter((l) => ["add", "del", "ctx"].includes(l.kind) && !(side === "left" && l.kind === "add") && !(side === "right" && l.kind === "del"))
    .map((l) => l.text)
    .join("\n");
}

/** Which side of a side-by-side diff a pointer event is on (null: a unified diff). */
export function sideOf(e: { target: EventTarget | null }): "left" | "right" | null {
  const td = (e.target as HTMLElement | null)?.closest?.("td");
  return td?.classList.contains("left") ? "left" : td?.classList.contains("right") ? "right" : null;
}

let current: string | null = null;
let installed = false;

function hasOwnSelection(): boolean {
  const a = document.activeElement as HTMLInputElement | HTMLTextAreaElement | null;
  if (a && (a.tagName === "TEXTAREA" || a.tagName === "INPUT")) return (a.selectionStart ?? 0) !== (a.selectionEnd ?? 0);
  const s = window.getSelection();
  return !!s && !s.isCollapsed && s.toString() !== "";
}

function install() {
  if (installed) return;
  installed = true;
  document.addEventListener("keydown", (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== "c") return;
    if (current === null || hasOwnSelection()) return; // selected text (or text in a box) copies as usual
    e.preventDefault();
    void writeClipboard(current).then((ok) => {
      if (ok) window.dispatchEvent(new CustomEvent("waw-diff-copied"));
    });
  });
}

/** While `text` is not null, Ctrl/⌘+C copies it (the diff's selected lines). */
export function useCopyLines(text: string | null) {
  useEffect(() => {
    install();
    if (text === null) return;
    current = text;
    return () => {
      if (current === text) current = null;
    };
  }, [text]);
}

/** "Copy" in a diff's composer: the selected lines' code. */
export function CopyLinesButton({ text, testId }: { text: string; testId: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className={`btn small ${done ? "done" : ""}`} disabled={!text} data-testid={testId} title="Copy the code of the selected lines (also Ctrl/⌘+C)"
      onClick={() => void writeClipboard(text).then((ok) => { if (ok) { setDone(true); window.setTimeout(() => setDone(false), 1500); } })}>
      {done ? "Copied ✓" : "Copy"}
    </button>
  );
}
