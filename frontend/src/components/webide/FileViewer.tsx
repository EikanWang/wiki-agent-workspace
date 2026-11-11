import { useEffect, useState } from "react";
import { Thread } from "../../api";
import PdfView, { PageAsk } from "../PdfView";

const IMAGE = /\.(png|jpe?g|gif|webp|bmp|ico|svg)$/i;

/** What kind of viewer a file that is not text gets. */
export function viewerKind(path: string): "image" | "pdf" | "binary" {
  if (IMAGE.test(path)) return "image";
  if (/\.pdf$/i.test(path)) return "pdf";
  return "binary";
}

/**
 * Files of a development workspace that are not text: images and PDFs are shown (like the wiki reader) and can be
 * discussed — the picture goes to the agent; a PDF page or a selected area of it too. Other files (archives,
 * libraries, models…) get their size and type, and a discussion about the file.
 */
export default function FileViewer({ devId, root, rel, threads, onDiscuss, onOpenThread }: {
  devId: string;
  root: string;
  rel: string;
  threads: Thread[];
  /** image: data URL of what is shown (page render / area crop); label: e.g. "page 3" */
  onDiscuss: (image: string | null, label?: string, quote?: string) => void;
  onOpenThread: (id: string) => void;
}) {
  const kind = viewerKind(rel);
  const url = `/api/v1/dev/workspaces/${devId}/fs/raw?root=${root}&path=${encodeURIComponent(rel)}`;
  const [zoom, setZoom] = useState(false);
  useEffect(() => setZoom(false), [url]);
  if (kind === "image") {
    return (
      <div className="webide-viewer" data-testid="webide-image">
        <div className="webide-md-bar"><span className="muted small">{rel}</span><span className="spacer" />
          <button className="btn small" onClick={() => onDiscuss(null)} data-testid="webide-discuss-image-btn">💬 Discuss this image</button></div>
        <div className={`image-view ${zoom ? "zoomed" : ""}`}>
          <img src={url} alt={rel} onClick={() => setZoom(!zoom)} title={zoom ? "Click to fit" : "Click for the actual size"} />
        </div>
      </div>
    );
  }
  if (kind === "pdf") {
    const mine = threads.filter((t) => {
      const a = t.target as unknown as { kind?: string; file?: string; root?: string } | null;
      return a?.kind === "code" && a.file === rel && (a.root ?? "wt") === root;
    });
    return (
      <div className="webide-viewer pdf-view" data-testid="webide-pdf">
        <div className="webide-md-bar"><span className="muted small">{rel} · ask about a page or an area with the buttons on each page</span><span className="spacer" />
          <button className="btn small" onClick={() => onDiscuss(null)} data-testid="webide-discuss-pdf">💬 Discuss this PDF</button></div>
        <PdfView url={url} unit="page" threads={[]} activeThreadId={null} testId="webide-pdfjs"
          onAsk={(a: PageAsk) => onDiscuss(a.image, a.target.label ?? `page ${a.target.page}`, a.quote)}
          onOpenThread={onOpenThread} />
        {mine.length > 0 && <div className="muted small webide-pad">{mine.length} discussion{mine.length === 1 ? "" : "s"} about this PDF in the agent panel</div>}
      </div>
    );
  }
  return (
    <div className="webide-empty" data-testid="webide-binary">
      <div className="webide-empty-title">{rel.split("/").pop()}</div>
      <div className="muted small">A binary file — not shown here. The agent can still inspect it (file type, symbols, contents) with its tools.</div>
      <div className="row"><button className="btn small primary" onClick={() => onDiscuss(null)} data-testid="webide-discuss-binary">💬 Discuss this file</button>
        <a className="btn small" href={url} download>Download</a></div>
    </div>
  );
}
