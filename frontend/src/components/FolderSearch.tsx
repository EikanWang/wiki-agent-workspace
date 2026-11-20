import { useEffect, useRef, useState } from "react";
import { ApiError, WorkspaceInfo } from "../api";
import { ContentDone, ContentFile, ContentHit, NameHit, findFragment, searchContent, searchNames } from "../search";
import { FileIcon } from "./FileIcon";
import { SearchIcon } from "./Icons";

export type SearchMode = "names" | "content";
export interface SearchScope {
  ws: string;
  path: string;
}

interface RootResult {
  ws: WorkspaceInfo;
  names?: NameHit[];
  namesTruncated?: boolean;
  files: ContentFile[];
  done?: ContentDone;
  progress?: { searched: number; total: number };
  extracting?: number;
  error?: string;
}

const NAME_DELAY_MS = 200;
const CONTENT_DELAY_MS = 450;

/**
 * Search in the open folders (above the tree): file and folder names, or the text inside the files (Markdown,
 * text, Word, PowerPoint, Visio, PDF). The results take the tree's place while there is a query; a match opens its
 * document with the find bar at that match.
 */
export default function FolderSearch({ workspaces, scope, onScope, query, onQuery, mode, onMode, focusKey, onOpen, onOpenMatch, onReveal }: {
  workspaces: WorkspaceInfo[];
  /** a folder to search in (else every open folder) */
  scope: SearchScope | null;
  onScope: (s: SearchScope | null) => void;
  query: string;
  onQuery: (q: string) => void;
  mode: SearchMode;
  onMode: (m: SearchMode) => void;
  /** bumped to focus the field (Ctrl+Shift+F) */
  focusKey: number;
  onOpen: (ws: string, path: string) => void;
  onOpenMatch: (ws: string, path: string, fragment: string) => void;
  /** show a folder in the tree */
  onReveal: (ws: string, path: string) => void;
}) {
  const [opts, setOpts] = useState({ case: false, word: false });
  const [results, setResults] = useState<RootResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [ran, setRan] = useState(""); // the search the results are for
  const input = useRef<HTMLInputElement>(null);
  const roots = workspaces.filter((w) => w.available && (!scope || w.id === scope.ws));
  const rootsKey = roots.map((w) => w.id).join(",");

  useEffect(() => {
    if (focusKey) {
      input.current?.focus();
      input.current?.select();
    }
  }, [focusKey]);

  useEffect(() => {
    const q = query.trim();
    if (!q || (mode === "content" && q.length < 2)) {
      setResults([]);
      setBusy(false);
      setRan("");
      return;
    }
    const ctl = new AbortController();
    const sub = scope?.path ?? "";
    const id = `${mode}\0${q}\0${opts.case}\0${opts.word}\0${rootsKey}\0${sub}`;
    const timer = window.setTimeout(() => {
      setBusy(true);
      const base: RootResult[] = roots.map((ws) => ({ ws, files: [] }));
      setResults(base);
      setFolded(new Set());
      setRan(id);
      const patch = (i: number, f: (r: RootResult) => RootResult) => {
        if (!ctl.signal.aborted) setResults((rs) => rs.map((r, k) => (k === i ? f(r) : r)));
      };
      const jobs = roots.map(async (ws, i) => {
        try {
          if (mode === "names") {
            const r = await searchNames(ws.id, sub, q);
            patch(i, (x) => ({ ...x, names: r.items, namesTruncated: r.truncated }));
          } else {
            await searchContent(ws.id, sub, q, opts, (e) => {
              if (e.type === "file") patch(i, (x) => ({ ...x, files: [...x.files, e] }));
              else if (e.type === "progress") patch(i, (x) => ({ ...x, progress: { searched: e.searched, total: e.total } }));
              else if (e.type === "status") patch(i, (x) => ({ ...x, extracting: e.extracting }));
              else patch(i, (x) => ({ ...x, done: e, extracting: undefined }));
            }, ctl.signal);
          }
        } catch (e) {
          if (ctl.signal.aborted) return;
          patch(i, (x) => ({ ...x, error: (e as ApiError).message || String(e) }));
        }
      });
      void Promise.all(jobs).then(() => !ctl.signal.aborted && setBusy(false));
    }, mode === "names" ? NAME_DELAY_MS : CONTENT_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      ctl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, mode, opts.case, opts.word, rootsKey, scope?.path]);

  const multi = results.length > 1;
  const scopeWs = scope ? workspaces.find((w) => w.id === scope.ws) : null;
  const q = query.trim();
  const total = results.reduce((a, r) => a + (mode === "names" ? r.names?.length ?? 0 : r.files.reduce((b, f) => b + f.count, 0)), 0);
  const nFiles = results.reduce((a, r) => a + r.files.length, 0);

  const snippet = (h: ContentHit) => (
    <>
      {h.cut[0] && "…"}
      {h.text.slice(0, h.s)}
      <mark>{h.text.slice(h.s, h.e)}</mark>
      {h.text.slice(h.e)}
      {h.cut[1] && "…"}
    </>
  );

  return (
    <div className={`folder-search ${q ? "active" : ""}`} data-testid="folder-search">
      <div className="search-box">
        <span className="search-icon" aria-hidden="true"><SearchIcon size={14} /></span>
        <input
          ref={input}
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              onQuery("");
            }
          }}
          placeholder={mode === "names" ? "Search file names (or *.docx)" : "Search in files"}
          aria-label={mode === "names" ? "Search file names" : "Search in files"}
          spellCheck={false}
          data-testid="search-input"
        />
        {mode === "content" && (
          <>
            <button className={`btn small toggle ${opts.case ? "on" : ""}`} aria-pressed={opts.case} onClick={() => setOpts((o) => ({ ...o, case: !o.case }))} title="Match case" data-testid="search-case">Aa</button>
            <button className={`btn small toggle ${opts.word ? "on" : ""}`} aria-pressed={opts.word} onClick={() => setOpts((o) => ({ ...o, word: !o.word }))} title="Whole words only" data-testid="search-word"><u>ab</u></button>
          </>
        )}
        {query && <button className="icon-btn small" onClick={() => onQuery("")} title="Clear the search (Esc)" aria-label="Clear the search" data-testid="search-clear">×</button>}
      </div>
      <div className="search-modes">
        <span className="mode-switch" role="tablist" aria-label="Search in">
          <button role="tab" aria-selected={mode === "names"} className={mode === "names" ? "active" : ""} onClick={() => onMode("names")} title="File and folder names" data-testid="search-mode-names">Names</button>
          <button role="tab" aria-selected={mode === "content"} className={mode === "content" ? "active" : ""} onClick={() => onMode("content")} title="The text in the files: Markdown, text, Word, PowerPoint, Visio, PDF (Ctrl+Shift+F)" data-testid="search-mode-content">Contents</button>
        </span>
        {scope && (
          <span className="search-scope chip" title={`Searching in ${scopeWs?.name ?? scope.ws}/${scope.path}`} data-testid="search-scope">
            in {scope.path ? scope.path.split("/").pop() : scopeWs?.name ?? "folder"}
            <button className="icon-btn small" onClick={() => onScope(null)} title="Search every open folder" aria-label="Search every open folder" data-testid="search-scope-clear">×</button>
          </span>
        )}
      </div>
      {q && ran && (
        <div className="search-summary muted small" data-testid="search-summary">
          {busy ? (mode === "content" ? searchingText(results) : "Searching…")
            : mode === "names" ? `${total || "No"} match${total === 1 ? "" : "es"}${results.some((r) => r.namesTruncated) ? " (the first ones)" : ""}`
              : `${total || "No"} match${total === 1 ? "" : "es"}${nFiles ? ` in ${nFiles} file${nFiles === 1 ? "" : "s"}` : ""}${results.some((r) => r.done?.truncated) ? " (the first files)" : ""}`}
          {!busy && mode === "content" && notes(results)}
        </div>
      )}
      {q && mode === "content" && q.length < 2 && <div className="search-summary muted small">Type at least 2 characters.</div>}
      <div className="search-results" data-testid="search-results">
        {results.map((r) => (
          <div key={r.ws.id} className="search-root" data-ws={r.ws.id}>
            {multi && ((r.names?.length ?? 0) > 0 || r.files.length > 0 || r.error) && <div className="search-root-name">{r.ws.name}</div>}
            {r.error && <div className="banner warn small">{r.error}</div>}
            {mode === "names" && r.names?.map((it) => {
              const dir = it.path.includes("/") ? it.path.slice(0, it.path.lastIndexOf("/")) : "";
              return (
                <button key={it.path} className={`search-name ${it.type === "file" && !it.openable ? "inert" : ""}`} title={it.path}
                  onClick={() => (it.type === "dir" ? onReveal(r.ws.id, it.path) : it.openable && onOpen(r.ws.id, it.path))} data-testid={`search-name-${it.path}`}>
                  <FileIcon item={it} />
                  <span className="name">{highlightTerms(it.name, q)}</span>
                  {dir && <span className="search-dir muted">{dir}</span>}
                </button>
              );
            })}
            {mode === "content" && r.files.map((f) => {
              const k = `${r.ws.id}\0${f.path}`;
              const open = !folded.has(k);
              const name = f.path.split("/").pop() ?? f.path;
              const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "";
              return (
                <div key={f.path} className="search-file" data-testid={`search-file-${f.path}`}>
                  <button className="search-file-head" title={f.path} onClick={() => setFolded((s) => { const n = new Set(s); if (n.has(k)) n.delete(k); else n.add(k); return n; })}>
                    <span className="chev">{open ? "⌄" : "›"}</span>
                    <FileIcon item={{ type: "file", kind: f.kind }} />
                    <span className="name">{name}</span>
                    {dir && <span className="search-dir muted">{dir}</span>}
                    <span className="search-count">{f.count}</span>
                  </button>
                  {open && f.hits.map((h) => (
                    <button key={h.n} className="search-hit" onClick={() => onOpenMatch(r.ws.id, f.path, findFragment(q, h, opts))} title={h.line ? `Line ${h.line}` : h.page ? `${f.format === "pptx" ? "Slide" : "Page"} ${h.page}` : ""} data-testid="search-hit">
                      {(h.line || h.page) && <span className="search-at muted">{h.line ?? `${f.format === "pptx" ? "s" : "p"}${h.page}`}</span>}
                      <span className="search-snippet">{snippet(h)}</span>
                    </button>
                  ))}
                  {open && f.count > f.hits.length && (
                    <button className="search-hit more muted" onClick={() => onOpenMatch(r.ws.id, f.path, findFragment(q, f.hits[f.hits.length - 1], opts))}>
                      {f.count - f.hits.length} more in this file…
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function searchingText(results: RootResult[]): string {
  const ext = results.reduce((a, r) => a + (r.extracting ?? 0), 0);
  const searched = results.reduce((a, r) => a + (r.done?.searched ?? r.progress?.searched ?? 0), 0);
  const all = results.reduce((a, r) => a + (r.done?.searched ?? r.progress?.total ?? 0), 0);
  if (ext && !all) return `Reading the text of ${ext} Word / PowerPoint / PDF file${ext === 1 ? "" : "s"}…`;
  return all ? `Searching… ${searched.toLocaleString()} of ${all.toLocaleString()} files` : "Searching…";
}

function notes(results: RootResult[]): React.ReactNode {
  const pending = results.reduce((a, r) => a + (r.done?.pending ?? 0), 0);
  const links = results.reduce((a, r) => a + (r.done?.links ?? 0), 0);
  const searched = results.reduce((a, r) => a + (r.done?.searched ?? 0), 0);
  return (
    <>
      {` · ${searched.toLocaleString()} files searched`}
      {pending > 0 && <div data-testid="search-pending">{pending.toLocaleString()} Word / PowerPoint / PDF file{pending === 1 ? " is" : "s are"} still being read and not searched yet; search again in a while.</div>}
      {links > 0 && <div title="Linked folders often lead to large collections that are open as folders of their own. Search in one to include it.">{links} linked folder{links === 1 ? "" : "s"} not searched.</div>}
    </>
  );
}

/** The name with the query's words marked. */
function highlightTerms(name: string, q: string): React.ReactNode {
  if (/[*?[]/.test(q)) return name;
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  const low = name.toLowerCase();
  const marks = new Array(name.length).fill(false);
  for (const t of terms) for (let i = low.indexOf(t); i >= 0; i = low.indexOf(t, i + 1)) for (let k = i; k < i + t.length; k++) marks[k] = true;
  const out: React.ReactNode[] = [];
  let i = 0;
  while (i < name.length) {
    let j = i;
    while (j < name.length && marks[j] === marks[i]) j++;
    out.push(marks[i] ? <mark key={i}>{name.slice(i, j)}</mark> : name.slice(i, j));
    i = j;
  }
  return out;
}
