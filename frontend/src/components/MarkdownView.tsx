import { useEffect, useMemo, useRef } from "react";
import { localRawUrl, renderMarkdown, showImages } from "../markdown";
import { isRefHref, linkifyPaths, useRefOpen } from "./Refs";

/** Sanitized Markdown rendering for agent messages (same markdown-it + DOMPurify pipeline as the reader).
 * In the app's window, links to files (and absolute paths in `code`) open the place they point at.
 * Images the answer shows or links to (PNG, GIF, SVG, …, and ```svg blocks) are drawn in it. */
export default function MarkdownView({ text, className = "" }: { text: string; className?: string }) {
  const openRef = useRefOpen();
  const box = useRef<HTMLDivElement>(null);
  const html = useMemo(() => {
    const h = renderMarkdown(text, "", { absoluteImageUrl: localRawUrl });
    return showImages(openRef ? linkifyPaths(h) : h);
  }, [text, openRef]);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    // a picture from a link that doesn't load (not an image this server has) is left out, the link stays
    const onError = (e: Event) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "IMG") t.closest(".msg-image-auto")?.classList.add("msg-image-broken");
    };
    el.addEventListener("error", onError, true);
    return () => el.removeEventListener("error", onError, true);
  }, []);
  const onClick = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") ?? null;
    if (a?.hasAttribute("data-image")) {
      if (href?.startsWith("data:")) {
        // browsers don't open data: URLs in a tab; the page shows it as an <img>, so the SVG's scripts never run
        e.preventDefault();
        const page = `<!doctype html><title>${a.title.replace(/[<&]/g, "")}</title><img src="${href.replace(/"/g, "%22")}" style="max-width:100%">`;
        window.open(URL.createObjectURL(new Blob([page], { type: "text/html" })), "_blank", "noopener");
      }
      return;
    }
    if (!openRef || !isRefHref(href) || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    openRef(href);
  };
  return <div ref={box} className={`markdown-body compact ${className}`} onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}
