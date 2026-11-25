import { useEffect, useState } from "react";

/** Clicking a picture (PNG, GIF, SVG, …) in agent answers, documents, PR text … opens it in a dialog in this
 * window instead of a new tab. It is shown as an <img>, so an SVG's scripts never run. */
export default function ImageLightbox() {
  const [img, setImg] = useState<{ src: string; alt: string } | null>(null);
  const [zoom, setZoom] = useState(1);
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target as HTMLElement;
      if (t.tagName !== "IMG" || t.closest(".svg-lightbox") || !t.closest(".markdown-body, a[data-image]")) return;
      const src = (t as HTMLImageElement).currentSrc || (t as HTMLImageElement).src;
      if (!src) return;
      e.preventDefault();
      e.stopPropagation();
      setZoom(1);
      setImg({ src, alt: (t as HTMLImageElement).alt || "Image" });
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, []);
  useEffect(() => {
    if (!img) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setImg(null); } };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [img]);
  if (!img) return null;
  return (
    <div className="modal-backdrop svg-lightbox" onClick={() => setImg(null)} data-testid="svg-lightbox">
      <div className="modal svg-lightbox-box" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={img.alt}>
        <div className="modal-head"><span>{img.alt}</span>
          <span className="spacer" />
          <button className="btn small" onClick={() => setZoom((z) => Math.max(0.25, z / 1.25))} title="Zoom out">−</button>
          <button className="btn small" onClick={() => setZoom(1)} title="Fit">{Math.round(zoom * 100)}%</button>
          <button className="btn small" onClick={() => setZoom((z) => Math.min(8, z * 1.25))} title="Zoom in">+</button>
          <button className="icon-btn" onClick={() => setImg(null)} title="Close (Esc)" data-testid="svg-lightbox-close">×</button>
        </div>
        <div className="svg-lightbox-view">
          <img src={img.src} alt={img.alt} style={zoom === 1 ? undefined : { width: `${zoom * 100}%`, maxWidth: "none", maxHeight: "none" }} />
        </div>
      </div>
    </div>
  );
}
