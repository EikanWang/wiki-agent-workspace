import { useEffect, useState } from "react";
import { ApiError, RecentItem, get } from "../api";

function timeAgo(iso: string): string {
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
}

/** Documents you worked on, newest activity first. Clicking restores document, position and discussion. */
export default function RecentList({ onOpen, compact = false, refreshKey = 0 }: { onOpen: (item: RecentItem) => void; compact?: boolean; refreshKey?: number }) {
  const [items, setItems] = useState<RecentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    get<{ items: RecentItem[] }>("/recent")
      .then((r) => setItems(r.items))
      .catch((e) => setError((e as ApiError).message));
  }, [refreshKey]);
  if (error) return <div className="error-text pad">{error}</div>;
  if (items === null) return <div className="muted pad">Loading…</div>;
  if (items.length === 0) return <div className="muted pad">Nothing yet. Documents you read or discuss appear here, newest first.</div>;
  return (
    <div className={`recent-list ${compact ? "compact" : ""}`} data-testid="recent-list">
      {items.map((it) => (
        <button key={it.document_id} className="recent-item" onClick={() => onOpen(it)} data-testid="recent-item" title={it.path}>
          <div className="recent-head">
            <span className="recent-path">{it.path}{it.missing ? " (missing)" : ""}</span>
            <span className="muted small" title={new Date(it.last_activity).toLocaleString()}>{timeAgo(it.last_activity)}</span>
          </div>
          <div className="recent-meta muted small">
            {it.reading_state?.heading ? <span>at "{it.reading_state.heading}"</span> : <span>top</span>}
            {it.reading_state && it.reading_state.scroll_fraction > 0 && <span> · {Math.round(it.reading_state.scroll_fraction * 100)}% read</span>}
            {it.thread_count > 0 && <span> · {it.thread_count} discussion{it.thread_count > 1 ? "s" : ""}, {it.message_count} messages</span>}
            {it.session && <span> · {it.session.provider}{it.session.short_id ? ` #${it.session.short_id}` : ""}</span>}
          </div>
          {it.last_thread_quote && <div className="recent-quote">"{it.last_thread_quote}"</div>}
          {it.last_message && (
            <div className="recent-last small">
              <b>{it.last_message.role === "user" ? "You" : "Agent"}:</b> {it.last_message.content}{it.last_message.content.length >= 160 ? "…" : ""}
              {it.last_message.status !== "completed" && <span className={`chip status ${it.last_message.status}`}>{it.last_message.status}</span>}
            </div>
          )}
        </button>
      ))}
    </div>
  );
}
