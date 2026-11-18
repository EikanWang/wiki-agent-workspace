import { useMemo } from "react";
import { renderMarkdown } from "../markdown";
import { isRefHref, linkifyPaths, useRefOpen } from "./Refs";

/** Sanitized Markdown rendering for agent messages (same markdown-it + DOMPurify pipeline as the reader).
 * In the app's window, links to files (and absolute paths in `code`) open the place they point at. */
export default function MarkdownView({ text, className = "" }: { text: string; className?: string }) {
  const openRef = useRefOpen();
  const html = useMemo(() => (openRef ? linkifyPaths(renderMarkdown(text, "")) : renderMarkdown(text, "")), [text, openRef]);
  const onClick = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a");
    const href = a?.getAttribute("href") ?? null;
    if (!openRef || !isRefHref(href) || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    openRef(href);
  };
  return <div className={`markdown-body compact ${className}`} onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}
