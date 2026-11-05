import { useEffect, useMemo, useState } from "react";
import { ApiError, RecentItem, del, get, put } from "../api";

function timeAgo(iso: string): string {
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
}

/** Documents you worked on, newest activity first. Clicking restores document, position and discussion.
 *  Entries can be hidden (kept, just not listed), unhidden, or deleted together with their history. */
export default function RecentList({ onOpen, compact = false, refreshKey = 0, onNotify }: { onOpen: (item: RecentItem) => void; compact?: boolean; refreshKey?: number; onNotify?: (msg: string) => void }) {
  const [items, setItems] = useState<RecentItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<RecentItem | null>(null);

  const load = async () => {
    try {
      const r = await get<{ items: RecentItem[] }>(`/recent?include_hidden=${showHidden}`);
      setItems(r.items);
      setError(null);
    } catch (e) {
      setError((e as ApiError).message);
    }
  };
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, showHidden]);

  const visible = useMemo(() => {
    if (!items) return null;
    const q = filter.trim().toLowerCase();
    if (!q) return items;
    return items.filter((it) => it.path.toLowerCase().includes(q) || (it.last_thread_quote ?? "").toLowerCase().includes(q) || (it.last_message?.content ?? "").toLowerCase().includes(q));
  }, [items, filter]);

  const setHidden = async (it: RecentItem, hidden: boolean) => {
    setBusy(it.document_id);
    try {
      await put(`/documents/${it.document_id}/hidden`, { hidden });
      await load();
      onNotify?.(hidden ? `Hidden ${it.path} from Recent (history kept).` : `${it.path} is shown again.`);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };

  const remove = async (it: RecentItem) => {
    setBusy(it.document_id);
    try {
      const r = await del<{ deleted: { threads: number; messages: number; versions: number } }>(`/documents/${it.document_id}/history`);
      setConfirmDelete(null);
      await load();
      onNotify?.(`Deleted history of ${it.path}: ${r.deleted.threads} discussions, ${r.deleted.messages} messages, ${r.deleted.versions} versions. The file itself was not touched.`);
    } catch (e) {
      const ae = e as ApiError;
      setError(ae.code === "DOCUMENT_BUSY" ? "An agent turn is still running for this document; cancel it first." : ae.message);
    } finally {
      setBusy(null);
    }
  };

  const hiddenCount = items?.filter((i) => i.hidden).length ?? 0;

  return (
    <div className={`recent ${compact ? "compact" : ""}`} data-testid="recent-list">
      <div className="recent-toolbar">
        <input type="search" placeholder="Filter by path or text…" value={filter} onChange={(e) => setFilter(e.target.value)} data-testid="recent-filter" />
        <label className="small"><input type="checkbox" checked={showHidden} onChange={(e) => setShowHidden(e.target.checked)} data-testid="recent-show-hidden" /> show hidden{showHidden && hiddenCount ? ` (${hiddenCount})` : ""}</label>
      </div>
      {error && <div className="error-text">{error}</div>}
      {visible === null && <div className="muted pad">Loading…</div>}
      {visible && visible.length === 0 && <div className="muted pad">{items && items.length ? "No entries match the filter." : "Nothing yet. Documents you read or discuss appear here, newest first."}</div>}
      {visible && visible.length > 0 && (
        <div className="recent-list">
          {visible.map((it) => (
            <div key={it.document_id} className={`recent-item ${it.hidden ? "hidden" : ""}`} data-testid="recent-item" data-hidden={it.hidden ? "1" : "0"}>
              <button className="recent-main" onClick={() => onOpen(it)} title={it.path}>
                <div className="recent-head">
                  <span className="recent-path">{it.path}{it.missing ? " (missing)" : ""}{it.hidden ? " · hidden" : ""}</span>
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
              <div className="recent-actions">
                {it.hidden ? (
                  <button className="icon-btn" disabled={busy === it.document_id} onClick={() => void setHidden(it, false)} title="Show in Recent again" data-testid="recent-unhide">👁</button>
                ) : (
                  <button className="icon-btn" disabled={busy === it.document_id} onClick={() => void setHidden(it, true)} title="Hide from Recent (keeps discussions and history)" data-testid="recent-hide">⌀</button>
                )}
                <button className="icon-btn danger" disabled={busy === it.document_id} onClick={() => setConfirmDelete(it)} title="Delete this document's discussions, versions and reading state (the file is not touched)" data-testid="recent-delete">🗑</button>
              </div>
            </div>
          ))}
        </div>
      )}
      {confirmDelete && (
        <div className="confirm-box" role="alertdialog" data-testid="recent-delete-confirm">
          <div>
            Delete the app history of <b>{confirmDelete.path}</b>? This removes {confirmDelete.thread_count} discussion{confirmDelete.thread_count === 1 ? "" : "s"} ({confirmDelete.message_count} messages), saved versions and the reading position. The Markdown file itself is <b>not</b> modified. This cannot be undone.
          </div>
          <div className="row">
            <button className="btn danger" onClick={() => void remove(confirmDelete)} data-testid="recent-delete-confirm-yes">Delete history</button>
            <button className="btn" onClick={() => setConfirmDelete(null)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
