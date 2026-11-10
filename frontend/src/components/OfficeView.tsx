import { useEffect, useRef, useState } from "react";
import { ApiError, Thread, get, getWorkspace } from "../api";
import PdfView, { PageAsk } from "./PdfView";

interface RenderState {
  state: "ready" | "pending" | "failed" | "unavailable";
  key?: string;
  seconds?: number;
  queued?: boolean;
  code?: string;
  message?: string;
}

const qs = (path: string, extra: Record<string, string> = {}) => {
  const p = new URLSearchParams({ path, ...extra });
  const ws = getWorkspace();
  if (ws) p.set("workspace", ws);
  return p.toString();
};

/**
 * Layout view of an Office file: the server renders it to PDF with LibreOffice (cached per content), the browser's
 * PDF viewer shows it, so pages, slides and diagrams look as their authors made them (zoom, page navigation, search).
 */
export default function OfficeView({ path, sha256, onShowText, unit, threads, activeThreadId, onAsk, onOpenThread }: {
  path: string;
  sha256: string;
  onShowText: (() => void) | null;
  unit: "page" | "slide";
  threads: Thread[];
  activeThreadId: string | null;
  onAsk: (a: PageAsk) => void;
  onOpenThread: (id: string) => void;
}) {
  const [st, setSt] = useState<RenderState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; window.clearTimeout(timer.current); }, []);

  const poll = async (retry = false) => {
    window.clearTimeout(timer.current);
    try {
      const r = await get<RenderState>(`/office/render?${qs(path, retry ? { retry: "true" } : {})}`);
      if (!alive.current) return;
      setSt(r);
      setError(null);
      if (r.state === "pending") timer.current = window.setTimeout(() => void poll(), 1000);
    } catch (e) {
      if (!alive.current) return;
      setError((e as ApiError).message);
      timer.current = window.setTimeout(() => void poll(), 5000); // e.g. server restarting: keep trying, slowly
    }
  };

  // (re)start when the document or its content changes
  useEffect(() => {
    setSt(null);
    void poll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, sha256]);

  const download = `/api/v1/raw?${qs(path)}`;
  const name = path.split("/").pop() ?? path;

  if (st?.state === "ready") {
    return (
      <div className="pdf-view office-view" data-testid="office-view" data-state="ready">
        <div className="muted small office-note">
          Original layout, rendered by LibreOffice. Select text, or ask about a whole {unit} or an area of it.{" "}
          <a href={download} download={name}>Download the original</a>
        </div>
        <PdfView url={`/api/v1/office/pdf?${qs(path, { k: st.key ?? "" })}`} unit={unit} threads={threads} activeThreadId={activeThreadId} onAsk={onAsk} onOpenThread={onOpenThread} testId="office-pdf" />
      </div>
    );
  }
  return (
    <div className="office-view office-status" data-testid="office-view" data-state={st?.state ?? "pending"}>
      {(!st || st.state === "pending") && !error && (
        <div className="office-pending">
          <span className="spinner" aria-hidden="true" />
          <div>
            <div>Rendering the original layout…</div>
            <div className="muted small">
              {st?.queued ? "Waiting for another document to finish. " : ""}
              {st?.seconds ? `${st.seconds} s · ` : ""}LibreOffice converts the file once; it opens instantly afterwards.
            </div>
          </div>
        </div>
      )}
      {error && <div className="banner warn">Could not reach the server: {error}. Retrying…</div>}
      {st?.state === "failed" && (
        <div className="banner warn" data-testid="office-failed">
          <div>
            <b>The original layout could not be shown.</b> {st.message}
          </div>
          {st.code !== "TOO_LARGE" && <button className="btn small" onClick={() => { setSt(null); void poll(true); }} data-testid="office-retry">Try again</button>}
        </div>
      )}
      {st?.state === "unavailable" && <div className="banner warn" data-testid="office-unavailable">{st.message}</div>}
      {(st?.state === "failed" || st?.state === "unavailable") && (
        <div className="office-actions">
          {onShowText && <button className="btn small" onClick={onShowText}>Show the text view</button>}
          <a className="btn small" href={download} download={name}>Download the original</a>
        </div>
      )}
    </div>
  );
}
