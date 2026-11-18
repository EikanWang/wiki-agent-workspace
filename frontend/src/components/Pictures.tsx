import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type React from "react";

/** A picture pasted (Ctrl+V) or dropped into a composer, sent with the next message. */
export interface Picture { url: string; name: string }
export const MAX_PICTURES = 6;
const MAX_PICTURE_BYTES = 12 * 1024 * 1024;

/** An image file as a data: URL the server accepts (PNG / JPEG / GIF / WebP; anything else becomes a PNG). */
async function pictureOf(f: File): Promise<string> {
  const url = await new Promise<string>((ok, fail) => {
    const r = new FileReader();
    r.onload = () => ok(String(r.result));
    r.onerror = () => fail(r.error);
    r.readAsDataURL(f);
  });
  if (/^data:image\/(png|jpeg|gif|webp);base64,/.test(url)) return url;
  const img = new Image();
  await new Promise((ok, fail) => { img.onload = ok; img.onerror = fail; img.src = url; });
  const c = document.createElement("canvas");
  c.width = img.naturalWidth;
  c.height = img.naturalHeight;
  c.getContext("2d")!.drawImage(img, 0, 0);
  return c.toDataURL("image/png");
}

const images = (files: FileList | File[]) => Array.from(files).filter((f) => f.type.startsWith("image/"));

/** The pictures of a composer: spread `handlers` on its textarea, show `<PictureStrip pics={…} />` above it,
 *  send `urls` with the message and `clear()` once it is sent. */
export function usePictures() {
  const [pictures, setPictures] = useState<Picture[]>([]);
  const [error, setError] = useState<string | null>(null);
  const count = useRef(0);
  count.current = pictures.length;
  const add = useCallback(async (files: File[]) => {
    const imgs = images(files);
    if (!imgs.length) return;
    setError(null);
    const room = MAX_PICTURES - count.current;
    if (imgs.length > room) setError(`At most ${MAX_PICTURES} pictures per message.`);
    const added: Picture[] = [];
    for (const f of imgs.slice(0, Math.max(0, room))) {
      try {
        const url = await pictureOf(f);
        if (url.length * 0.75 > MAX_PICTURE_BYTES) { setError(`${f.name || "The picture"} is larger than 12 MiB.`); continue; }
        added.push({ url, name: f.name || `pasted.${f.type.split("/")[1] ?? "png"}` });
      } catch {
        setError(`Could not read ${f.name || "the pasted picture"}.`);
      }
    }
    setPictures((p) => [...p, ...added].slice(0, MAX_PICTURES));
  }, []);
  const remove = useCallback((n: number) => setPictures((p) => p.filter((_, k) => k !== n)), []);
  const clear = useCallback(() => { setPictures([]); setError(null); }, []);
  const handlers = {
    onPaste: (e: React.ClipboardEvent<HTMLTextAreaElement | HTMLInputElement>) => {
      const files = images(e.clipboardData.files);
      if (!files.length) return;
      e.preventDefault(); // the picture, not its file name; text copied along with it still goes into the box
      const plain = e.clipboardData.getData("text/plain");
      if (plain) document.execCommand("insertText", false, plain);
      void add(files);
    },
    onDragOver: (e: React.DragEvent<HTMLTextAreaElement | HTMLInputElement>) => {
      if (Array.from(e.dataTransfer.items).some((i) => i.type.startsWith("image/"))) e.preventDefault();
    },
    onDrop: (e: React.DragEvent<HTMLTextAreaElement | HTMLInputElement>) => {
      const files = images(e.dataTransfer.files);
      if (!files.length) return;
      e.preventDefault();
      void add(files);
    },
  };
  return { pictures, urls: pictures.map((p) => p.url), add, remove, clear, error, handlers };
}

export type Pictures = ReturnType<typeof usePictures>;

/** The pictures waiting to be sent, each with a × to take it out. */
export function PictureStrip({ pics }: { pics: Pictures }) {
  if (!pics.pictures.length && !pics.error) return null;
  return (
    <div className="composer-pictures" data-testid="composer-pictures">
      {pics.pictures.map((p, n) => (
        <span key={n} className="composer-picture" title={p.name} data-testid="composer-picture">
          <img src={p.url} alt={p.name} />
          <button type="button" className="icon-btn small" onClick={() => pics.remove(n)} aria-label={`Remove ${p.name}`} data-testid="composer-picture-remove">×</button>
        </span>
      ))}
      {pics.pictures.length > 0 && <span className="muted small">sent with your message</span>}
      {pics.error && <span className="warn small" role="alert">{pics.error}</span>}
    </div>
  );
}

/** The pictures sent with a message of a discussion (thumbnails; a click shows one in a dialog). */
export function MessageImages({ threadId, names }: { threadId?: string; names?: string[] }) {
  const [shown, setShown] = useState<number | null>(null);
  if (!threadId || !names?.length) return null;
  const srcs = names.map((n) => `/api/v1/threads/${threadId}/attachments/${n}`);
  return (
    <div className="msg-images" data-testid="msg-images">
      {srcs.map((src, k) => (
        <button key={src} type="button" className="msg-image-btn" onClick={() => setShown(k)} title="Show the picture">
          <img src={src} alt="sent with this message" data-testid="msg-image" />
        </button>
      ))}
      {shown !== null && <PictureViewer srcs={srcs} index={shown} onIndex={setShown} onClose={() => setShown(null)} />}
    </div>
  );
}

/** A picture over the page: Esc or a click outside closes it; ← / → go through the message's pictures. */
function PictureViewer({ srcs, index, onIndex, onClose }: { srcs: string[]; index: number; onIndex: (k: number) => void; onClose: () => void }) {
  const many = srcs.length > 1;
  const go = (d: number) => onIndex((index + d + srcs.length) % srcs.length);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); }
      else if (many && e.key === "ArrowLeft") go(-1);
      else if (many && e.key === "ArrowRight") go(1);
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  });
  return createPortal(
    <div className="modal-backdrop picture-viewer-backdrop" onClick={onClose} data-testid="picture-viewer">
      <div className="picture-viewer" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span className="muted small">{many ? `Picture ${index + 1} of ${srcs.length}` : "Picture"}</span>
          <span className="spacer" />
          {many && <button type="button" className="icon-btn" onClick={() => go(-1)} aria-label="Previous picture" data-testid="picture-viewer-prev">‹</button>}
          {many && <button type="button" className="icon-btn" onClick={() => go(1)} aria-label="Next picture" data-testid="picture-viewer-next">›</button>}
          <button type="button" className="icon-btn" onClick={onClose} aria-label="Close" data-testid="picture-viewer-close">×</button>
        </div>
        <img src={srcs[index]} alt={`picture ${index + 1}`} data-testid="picture-viewer-image" />
      </div>
    </div>,
    document.body,
  );
}

/** The text sent when a message has only pictures. */
export const picturesOnly = (n: number) => `Please look at the attached picture${n > 1 ? "s" : ""}.`;

/** The composer's placeholder, with the hint that pictures can be pasted. */
export const PASTE_HINT = "Ctrl+V pastes a picture";
