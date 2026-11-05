// API client: same-origin fetch with CSRF header, stable error shape, SSE helpers.

export interface ApiErrorBody {
  error: { code: string; message: string; details: Record<string, unknown> };
}

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;
  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

let csrfToken: string | null = null;
export function setCsrf(token: string | null) {
  csrfToken = token;
}
export function getCsrf(): string | null {
  return csrfToken;
}

const BASE = "/api/v1";

export async function api<T>(method: string, path: string, body?: unknown, opts: { idempotencyKey?: string } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  const resp = await fetch(BASE + path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await resp.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!resp.ok) {
    const e = (data as ApiErrorBody | null)?.error;
    throw new ApiError(resp.status, e?.code ?? "HTTP_" + resp.status, e?.message ?? resp.statusText, e?.details ?? {});
  }
  return data as T;
}

export const get = <T,>(path: string) => api<T>("GET", path);
export const post = <T,>(path: string, body?: unknown, opts?: { idempotencyKey?: string }) => api<T>("POST", path, body ?? {}, opts);
export const put = <T,>(path: string, body?: unknown) => api<T>("PUT", path, body ?? {});
export const patch = <T,>(path: string, body?: unknown) => api<T>("PATCH", path, body ?? {});
export const del = <T,>(path: string) => api<T>("DELETE", path);

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

// ---------------------------------------------------------------- types

export interface Capabilities {
  provider: string;
  available: boolean;
  version: string | null;
  executable: string | null;
  mode: string;
  can_list_sessions: boolean;
  can_resume: boolean;
  can_stream_deltas: boolean;
  can_cancel: boolean;
  can_relay_permissions: boolean;
  uses_local_service: boolean;
  eager_session_ids: boolean;
  preview_isolation: string;
  error: string | null;
  notes: string[];
}

export interface TerminalInfo {
  id: string;
  label: string;
  cwd: string;
  tab_order: number;
  state: "live" | "exited" | "unavailable" | "closed";
  tmux_session: string;
}

export interface Bootstrap {
  workspace: { id: string; root: string; name: string; git: GitSummary };
  default_provider: string | null;
  providers: Record<string, Capabilities>;
  terminals: TerminalInfo[];
  terminal_info: { tmux_available: boolean; tmux_version: string | null; inner_term: string; locale_warning: string | null; history_limit: number; install_hint: string | null };
  settings: { codex_sandbox: string; opencode_mode: string; max_document_bytes: number; watcher_error: string | null };
  watcher: { ready: boolean; error: string | null; registering_seconds: number | null } | null;
  csrf_token: string | null;
  default_provider_warning: string | null;
}

export interface GitSummary {
  is_repo: boolean;
  branch: string | null;
  commit: string | null;
  remote_state: string | null;
  error: string | null;
  file_status?: string | null;
}

export interface FileItem {
  path: string;
  name: string;
  type: "dir" | "file";
  /** dir | markdown | image | pdf | notebook | code | shell | data | text | archive | binary | file | broken_link */
  kind: string;
  size?: number | null;
  openable?: boolean;
  git_status: string | null;
  symlink?: boolean;
  link_target?: string | null;
  escapes?: boolean;
}

export interface ReadingState {
  version_sha256: string | null;
  heading: string | null;
  offset: number;
  scroll_fraction: number;
  active_thread_id: string | null;
  updated_at: string;
}

export interface RecentItem {
  document_id: string;
  path: string;
  missing: boolean;
  hidden: boolean;
  last_activity: string;
  thread_count: number;
  message_count: number;
  last_thread_id: string | null;
  last_thread_quote: string | null;
  last_message: { role: string; content: string; status: string; created_at: string } | null;
  reading_state: ReadingState | null;
  session: { provider: string; short_id: string | null; display_name: string | null; status: string } | null;
}

export interface OpenDocument {
  document_id: string;
  path: string;
  text: string;
  sha256: string;
  git: GitSummary;
  reading_state: ReadingState | null;
  default_session_id: string | null;
  missing: boolean;
}

export interface Anchor {
  version_sha256: string;
  quote: string;
  prefix: string;
  suffix: string;
  start: number;
  end: number;
  heading: string | null;
}

export interface Message {
  id: string;
  role: "user" | "agent" | "system";
  content: string;
  status: string;
  session_id: string | null;
  job_id: string | null;
  created_at: string;
  error_code: string | null;
  error_message: string | null;
}

export interface Thread {
  id: string;
  document_id: string;
  original_anchor: Anchor;
  anchor: Anchor;
  anchor_state: "attached" | "moved" | "ambiguous" | "orphaned";
  diff?: { base: string; target: string; side: string; line_no: number | null; line_text: string; anchored: boolean } | null;
  current_start: number | null;
  current_end: number | null;
  candidates?: number;
  last_session_id: string | null;
  created_at: string;
  updated_at: string;
  messages: Message[];
}

export interface AgentSession {
  id: string;
  provider: string;
  native_id: string | null;
  workspace_id: string;
  display_name: string | null;
  status: string;
  origin: string;
  short_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProviderSession {
  provider: string;
  native_id: string;
  title: string | null;
  directory: string | null;
  updated_at: string | null;
  linked: boolean;
}

export interface SessionList {
  sessions: AgentSession[];
  provider_sessions: ProviderSession[];
  capabilities: Record<string, Capabilities>;
  errors: Record<string, string>;
}

export interface JobEvent {
  seq: number;
  type: string;
  payload: Record<string, any>;
  job_id: string;
  created_at?: string;
}

export interface ReviewMark {
  id: string;
  quote: string;
  start: number;
  end: number;
  state: "reviewed" | "needs_review";
  anchor_state: string;
  current_start: number | null;
  current_end: number | null;
  reviewed_version_sha256: string;
}

export interface Preview {
  preview_id: string;
  document_id: string;
  job_id: string;
  status: "pending" | "ready" | "applied" | "failed" | "stale";
  base_sha256: string;
  proposed_sha256: string | null;
  unified?: string;
  identical?: boolean;
  stale?: boolean;
  current_sha256?: string;
  job_status: string | null;
  job_error: string | null;
}

// ---------------------------------------------------------------- SSE

export function subscribeJob(jobId: string, after: number, onEvent: (ev: JobEvent) => void, onEnd: (err?: string) => void): () => void {
  const es = new EventSource(`${BASE}/jobs/${jobId}/events?after=${after}`);
  let closed = false;
  const handler = (e: MessageEvent) => {
    try {
      const ev = JSON.parse(e.data) as JobEvent;
      onEvent(ev);
      if (ev.type === "completed" || ev.type === "failed" || ev.type === "cancelled") {
        closed = true;
        es.close();
        onEnd();
      }
    } catch {
      /* ignore */
    }
  };
  for (const t of ["queued", "waiting", "running", "started", "delta", "tool_activity", "notice", "permission_request", "completed", "failed", "cancelled"]) {
    es.addEventListener(t, handler as EventListener);
  }
  es.onerror = () => {
    if (!closed) {
      // EventSource reconnects automatically; the server replays from `after`, we dedupe by seq upstream.
    }
  };
  return () => {
    closed = true;
    es.close();
  };
}

export function subscribeGlobal(onEvent: (ev: Record<string, any>) => void): () => void {
  const es = new EventSource(`${BASE}/events`);
  const handler = (e: MessageEvent) => {
    try {
      onEvent(JSON.parse(e.data));
    } catch {
      /* ignore */
    }
  };
  for (const t of ["file_changed", "tree_changed", "job_event", "terminal_state", "preview_update", "watcher_ready", "message"]) {
    es.addEventListener(t, handler as EventListener);
  }
  return () => es.close();
}

// ---------------------------------------------------------------- diff review

export interface DiffFile {
  path: string;
  status: "added" | "deleted" | "modified" | "untracked" | string;
  unified: string;
  additions: number;
  deletions: number;
  identical: boolean;
  document_id: string | null;
  target_sha256: string | null;
  is_markdown: boolean;
}

export interface DiffComment {
  thread_id: string;
  diff: { base: string; target: string; side: string; line_no: number | null; line_text: string; anchored: boolean };
  question: string;
  last_status: string | null;
  message_count: number;
}

export interface DiffResult {
  path: string;
  is_dir: boolean;
  base: string;
  target: string;
  git: GitSummary;
  files: DiffFile[];
  summary: { files: number; additions: number; deletions: number };
  comments: Record<string, DiffComment[]>;
}

export interface GitRefs {
  commits: { sha: string; short: string; subject: string; date: string }[];
  branches: string[];
}
