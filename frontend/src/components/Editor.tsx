import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, OpenDocument, put } from "../api";
import { renderMarkdown } from "../markdown";

/**
 * In-browser editor for text documents (Markdown and any text/source file).
 * - Ctrl/⌘+S saves, Esc leaves (asks when there are unsaved changes), Tab indents.
 * - Saves are conditional on the hash the editor was opened with: if the file changed on disk meanwhile (agent,
 *   terminal, Git) the save is refused and you choose: reload theirs, or overwrite with yours.
 * - Markdown gets an optional live preview side by side.
 */
export default function Editor({ doc, externalChange, onSaved, onClose, onDirtyChange }: {
  doc: OpenDocument;
  /** the file changed on disk while editing (from the watcher) */
  externalChange: boolean;
  onSaved: (sha256: string) => void;
  onClose: () => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const [text, setText] = useState(doc.text);
  const [base, setBase] = useState(doc.sha256);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null); // current sha on disk
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [wrap, setWrap] = useState(() => localStorage.getItem("waw.editorWrap") !== "0");
  const [preview, setPreview] = useState(() => doc.format === "markdown" && localStorage.getItem("waw.editorPreview") === "1");
  const taRef = useRef<HTMLTextAreaElement>(null);
  const gutterRef = useRef<HTMLDivElement>(null);
  // "dirty" = differs from the last content this editor loaded or saved
  const savedTextRef = useRef(doc.text);
  const isDirty = text !== savedTextRef.current;
  useEffect(() => onDirtyChange(isDirty), [isDirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange(false), [onDirtyChange]);

  useEffect(() => {
    localStorage.setItem("waw.editorWrap", wrap ? "1" : "0");
  }, [wrap]);
  useEffect(() => {
    localStorage.setItem("waw.editorPreview", preview ? "1" : "0");
  }, [preview]);
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (!isDirty) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [isDirty]);
  useEffect(() => {
    taRef.current?.focus({ preventScroll: true });
  }, []);

  const indent = useMemo(() => (/^\t/m.test(doc.text) ? "\t" : "  "), [doc.text]);

  const save = useCallback(
    async (overwriteWith?: string) => {
      setBusy(true);
      setError(null);
      try {
        const r = await put<{ sha256: string; changed: boolean }>(`/documents/${doc.document_id}/content`, { text, base_sha256: overwriteWith ?? base });
        savedTextRef.current = text;
        setBase(r.sha256);
        setConflict(null);
        setSavedAt(Date.now());
        onDirtyChange(false);
        onSaved(r.sha256);
      } catch (e) {
        const ae = e as ApiError;
        if (ae.code === "DOCUMENT_VERSION_CONFLICT") setConflict(String(ae.details.current_sha256 ?? ""));
        else if (ae.code === "PATH_LOCKED") setError("This file is locked; unlock it to save. Your text is still here.");
        else setError(`${ae.code}: ${ae.message}`);
      } finally {
        setBusy(false);
      }
    },
    [doc.document_id, text, base, onSaved, onDirtyChange],
  );

  const close = () => {
    if (isDirty && !window.confirm("Discard your unsaved changes?")) return;
    onDirtyChange(false);
    onClose();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (!busy) void save();
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (e.key === "Tab" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      const ta = e.currentTarget;
      const { selectionStart: s, selectionEnd: en, value } = ta;
      if (s === en && !e.shiftKey) {
        const next = value.slice(0, s) + indent + value.slice(en);
        ta.value = next;
        ta.selectionStart = ta.selectionEnd = s + indent.length;
        setText(next);
        return;
      }
      // indent / outdent every selected line
      const lineStart = value.lastIndexOf("\n", s - 1) + 1;
      const block = value.slice(lineStart, en);
      const lines = block.split("\n");
      const changed = e.shiftKey
        ? lines.map((l) => (l.startsWith(indent) ? l.slice(indent.length) : l.replace(/^[ \t]/, "")))
        : lines.map((l) => indent + l);
      const replaced = changed.join("\n");
      const next = value.slice(0, lineStart) + replaced + value.slice(en);
      ta.value = next;
      ta.selectionStart = lineStart;
      ta.selectionEnd = lineStart + replaced.length;
      setText(next);
    }
  };

  const lineCount = useMemo(() => text.split("\n").length, [text]);
  const previewHtml = useMemo(() => (preview ? renderMarkdown(text, doc.path) : ""), [preview, text, doc.path]);

  return (
    <div className="editor" data-testid="editor">
      <div className="editor-bar">
        <span className="chip">EDITING</span>
        <span className={`muted small ${isDirty ? "warn" : ""}`} data-testid="editor-state">
          {busy ? "Saving…" : isDirty ? "Unsaved changes" : savedAt ? "Saved ✓" : "No changes"}
        </span>
        <span className="spacer" />
        <label className="small"><input type="checkbox" checked={wrap} onChange={(e) => setWrap(e.target.checked)} /> wrap</label>
        {doc.format === "markdown" && (
          <label className="small"><input type="checkbox" checked={preview} onChange={(e) => setPreview(e.target.checked)} data-testid="editor-preview-toggle" /> preview</label>
        )}
        <button className="btn small primary" disabled={busy || (!isDirty && !conflict)} onClick={() => void save()} data-testid="editor-save" title="Save (Ctrl/⌘+S)">Save</button>
        <button className="btn small" onClick={close} data-testid="editor-close" title="Back to reading (Esc)">Done</button>
      </div>
      {externalChange && !conflict && (
        <div className="banner warn small" data-testid="editor-external-change">
          The file changed on disk while you are editing. Saving will ask whether to overwrite it.
        </div>
      )}
      {conflict !== null && (
        <div className="banner warn" data-testid="editor-conflict">
          The file was changed on disk after you started editing (agent, terminal or Git). Your text is still here.
          <button className="btn small" onClick={() => { if (window.confirm("Replace the file on disk with your text?")) void save(conflict); }} data-testid="editor-overwrite">Overwrite with mine</button>
          <button className="btn small" onClick={() => { savedTextRef.current = text; onDirtyChange(false); onClose(); }} data-testid="editor-discard">Discard mine, reload</button>
          <button className="btn small" onClick={() => void navigator.clipboard?.writeText(text)}>Copy my text</button>
        </div>
      )}
      {error && <div className="banner warn" role="alert">{error}</div>}
      <div className={`editor-body ${preview ? "split" : ""}`}>
        <div className={`editor-pane ${wrap ? "wrap" : "nowrap"}`}>
          {!wrap && (
            <div className="editor-gutter" ref={gutterRef} aria-hidden="true">
              {Array.from({ length: lineCount }, (_, i) => (
                <div key={i}>{i + 1}</div>
              ))}
            </div>
          )}
          <textarea
            ref={taRef}
            className="editor-text"
            value={text}
            spellCheck={doc.format === "markdown"}
            wrap={wrap ? "soft" : "off"}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            onScroll={(e) => {
              if (gutterRef.current) gutterRef.current.scrollTop = e.currentTarget.scrollTop;
            }}
            data-testid="editor-text"
          />
        </div>
        {preview && <article className="markdown-body editor-preview" data-testid="editor-preview" dangerouslySetInnerHTML={{ __html: previewHtml }} />}
      </div>
    </div>
  );
}
