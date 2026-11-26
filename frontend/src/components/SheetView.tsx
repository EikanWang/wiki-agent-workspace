import { useEffect, useState } from "react";
import { ApiError, get } from "../api";

interface Sheet { name: string; rows: string[][]; truncated_rows: boolean; truncated_cols: boolean }

const colName = (i: number) => { let s = ""; for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s; return s; };

/** An Excel workbook (.xlsx) shown as tables, one tab per sheet (cell values only). */
export default function SheetView({ path, download }: { path: string; download: string }) {
  const [sheets, setSheets] = useState<Sheet[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [cur, setCur] = useState(0);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    setSheets(null); setErr(null); setCur(0);
    get<{ sheets: Sheet[] }>(`/local/sheet?path=${encodeURIComponent(path)}`).then((r) => setSheets(r.sheets)).catch((e) => setErr((e as ApiError).message));
  }, [path]);
  if (err) return <div className="webide-empty"><div className="error-text">{err}</div><a className="btn small" href={download} download>Download</a></div>;
  if (!sheets) return <div className="webide-empty muted small">Reading the workbook…</div>;
  const sh = sheets[cur];
  const width = sh ? Math.max(0, ...sh.rows.map((r) => r.length)) : 0;
  const q = filter.trim().toLowerCase();
  const rows = (sh?.rows ?? []).map((r, i) => [i, r] as const).filter(([, r]) => !q || r.some((c) => c.toLowerCase().includes(q)));
  return (
    <div className="sheet-view" data-testid="sheet-view">
      <div className="sheet-bar">
        <div className="sheet-tabs">
          {sheets.map((s, i) => <button key={i} className={`sheet-tab${i === cur ? " active" : ""}`} onClick={() => setCur(i)} data-testid="sheet-tab">{s.name}</button>)}
        </div>
        <input className="sheet-filter" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter rows…" data-testid="sheet-filter" />
        <a className="btn small" href={download} download>Download</a>
      </div>
      {sh && (sh.truncated_rows || sh.truncated_cols) && <div className="muted small">Only the first 2000 rows and 200 columns are shown; download the workbook for the rest.</div>}
      <div className="sheet-scroll">
        <table className="sheet-table" data-testid="sheet-table">
          <thead><tr><th />{Array.from({ length: width }, (_, i) => <th key={i}>{colName(i)}</th>)}</tr></thead>
          <tbody>
            {rows.map(([i, r]) => (
              <tr key={i}><th>{i + 1}</th>{Array.from({ length: width }, (_, j) => <td key={j} title={r[j] && r[j].length > 40 ? r[j] : undefined}>{r[j] ?? ""}</td>)}</tr>
            ))}
          </tbody>
        </table>
        {sh && sh.rows.length === 0 && <div className="muted small">This sheet is empty.</div>}
      </div>
    </div>
  );
}
