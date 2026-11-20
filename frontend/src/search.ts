import { ApiError, getIn } from "./api";

export interface NameHit {
  path: string;
  name: string;
  type: "dir" | "file";
  kind: string;
  format?: string;
  openable?: boolean;
}

export interface ContentHit {
  /** the n-th match in the file (from 0) */
  n: number;
  /** the line around the match; `s`..`e` is the match in it */
  text: string;
  s: number;
  e: number;
  /** the text just before the match (to find the same place in the shown document) */
  ctx: string;
  /** the snippet is cut at the start / end of its line */
  cut: [boolean, boolean];
  line?: number;
  page?: number;
}

export interface ContentFile {
  type: "file";
  path: string;
  format: string;
  kind: string;
  count: number;
  hits: ContentHit[];
}

export interface ContentDone {
  type: "done";
  files: number;
  hits: number;
  truncated: boolean;
  searched: number;
  /** Word / PowerPoint / PDF files in the folder whose text is not extracted yet (not searched) */
  pending: number;
  /** files of the folder still waiting for their text in the background */
  indexing: number;
  /** linked folders not searched */
  links: number;
  ms: number;
}

export type ContentEvent =
  | ContentFile
  | ContentDone
  | { type: "progress"; searched: number; total: number }
  | { type: "status"; extracting: number };

export const searchNames = (ws: string, path: string, q: string) =>
  getIn<{ items: NameHit[]; truncated: boolean }>(ws, `/search/names?q=${encodeURIComponent(q)}&path=${encodeURIComponent(path)}`);

/** The content search of one folder, its events as they come; stops when `signal` aborts. */
export async function searchContent(ws: string, path: string, q: string, opts: { case: boolean; word: boolean }, onEvent: (e: ContentEvent) => void, signal: AbortSignal): Promise<void> {
  const qs = new URLSearchParams({ q, path, case: String(opts.case), word: String(opts.word) });
  const resp = await fetch(`/api/v1/search/content?${qs}`, { headers: { Accept: "application/x-ndjson", "X-Workspace": ws }, credentials: "same-origin", signal });
  if (!resp.ok || !resp.body) {
    let err: { code?: string; message?: string } = {};
    try {
      err = (await resp.json())?.error ?? {};
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, err.code ?? `HTTP_${resp.status}`, err.message ?? resp.statusText, {});
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) onEvent(JSON.parse(line) as ContentEvent);
    }
  }
}

/** The fragment that opens a document at a search match: the find bar shows it (`n`-th match, else by the text before it). */
export function findFragment(q: string, hit: { n: number; ctx: string; page?: number }, opts: { case: boolean; word: boolean }): string {
  const p = new URLSearchParams({ "waw-find": q, n: String(hit.n), ctx: hit.ctx.slice(-40) });
  if (opts.case) p.set("case", "1");
  if (opts.word) p.set("word", "1");
  if (hit.page) p.set("page", String(hit.page));
  return p.toString();
}

export interface FindRequest {
  q: string;
  n: number;
  ctx: string;
  case: boolean;
  word: boolean;
  page: number | null;
}

export function parseFindFragment(fragment: string): FindRequest | null {
  const f = fragment.replace(/^#/, "");
  if (!f.startsWith("waw-find=")) return null;
  const p = new URLSearchParams(f);
  const q = p.get("waw-find") ?? "";
  if (!q) return null;
  return { q, n: Number(p.get("n") ?? 0) || 0, ctx: p.get("ctx") ?? "", case: p.get("case") === "1", word: p.get("word") === "1", page: Number(p.get("page")) || null };
}
