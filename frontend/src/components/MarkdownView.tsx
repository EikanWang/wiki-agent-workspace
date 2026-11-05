import { useMemo } from "react";
import { renderMarkdown } from "../markdown";

/** Sanitized Markdown rendering for agent messages (same markdown-it + DOMPurify pipeline as the reader). */
export default function MarkdownView({ text, className = "" }: { text: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(text, ""), [text]);
  return <div className={`markdown-body compact ${className}`} dangerouslySetInnerHTML={{ __html: html }} />;
}
