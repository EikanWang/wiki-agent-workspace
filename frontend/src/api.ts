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
// The root folder (workspace) requests are scoped to; sent as X-Workspace on every API call.
let workspaceId: string | null = null;
export function setWorkspace(id: string | null) {
  workspaceId = id;
}
export function getWorkspace(): string | null {
  return workspaceId;
}
export function setCsrf(token: string | null) {
  csrfToken = token;
}
export function getCsrf(): string | null {
  return csrfToken;
}

const BASE = "/api/v1";

export async function api<T>(method: string, path: string, body?: unknown, opts: { idempotencyKey?: string; workspace?: string | null; timeoutMs?: number; keepalive?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const ws = opts.workspace !== undefined ? opts.workspace : workspaceId;
  if (ws) headers["X-Workspace"] = ws;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  const ctl = opts.timeoutMs ? new AbortController() : null;
  const timer = ctl ? window.setTimeout(() => ctl.abort(), opts.timeoutMs) : undefined;
  let resp: Response;
  let text: string;
  try {
    resp = await fetch(BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      signal: ctl?.signal,
      keepalive: opts.keepalive, // still sent while the page closes / reloads
    });
    text = await resp.text();
  } catch (e) {
    // fetch() rejects only when no HTTP response arrived: server unreachable, connection dropped, or our timeout
    if (ctl?.signal.aborted) throw new ApiError(0, "TIMEOUT", `no answer from the server within ${Math.round(opts.timeoutMs! / 1000)} s`, {});
    throw new ApiError(0, "NETWORK", `cannot reach the server (${(e as Error).message})`, {});
  } finally {
    window.clearTimeout(timer);
  }
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

/** A named set of folders (VS Code "workspace"); exactly one is current. */
export interface NamedWorkspace {
  id: string;
  name: string;
  current: boolean;
  is_default: boolean;
  folders: { id: string; name: string; root: string }[];
  last_opened_at: string | null;
}
export interface NamedWorkspaceList {
  workspaces: NamedWorkspace[];
  current: string;
}

export const get = <T,>(path: string) => api<T>("GET", path);
/** GET scoped to a specific root folder (e.g. the multi-root file tree loading a non-active root). */
export const getIn = <T,>(workspace: string, path: string, opts: { timeoutMs?: number } = {}) => api<T>("GET", path, undefined, { workspace, ...opts });
export const post = <T,>(path: string, body?: unknown, opts?: { idempotencyKey?: string; workspace?: string | null }) => api<T>("POST", path, body ?? {}, opts);
export const put = <T,>(path: string, body?: unknown, opts?: { keepalive?: boolean }) => api<T>("PUT", path, body ?? {}, opts);
export const patch = <T,>(path: string, body?: unknown) => api<T>("PATCH", path, body ?? {});
export const del = <T,>(path: string, opts?: { workspace?: string | null }) => api<T>("DELETE", path, undefined, opts);

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

export interface MachineHop {
  name: string;
  kind: string;
  ssh: string | null;
  ok: boolean;
  status: string;
  message: string | null;
  error?: string;
  ms: number;
}

export interface MachineCheck {
  ok: boolean;
  status: string;
  failed_at: string | null;
  summary: string;
  hops: MachineHop[];
  checked_at: string;
}

export interface MachineGpu {
  vendor: string;
  index: number | null;
  name: string | null;
  uuid: string | null;
  pci: string;
  driver: string | null;
  memory: string | null;
  source: string;
}

export interface MachineTool { path: string | null; version: string | null; ok: boolean; required: boolean; for: string }

export interface MachineCaps {
  os: { kernel: string | null; release: string | null; arch: string | null; hostname: string | null; pretty: string | null };
  cpu: { model: string | null; threads: number | null; sockets: number | null; cores_per_socket: number | null; threads_per_core: number | null; numa_nodes: number | null };
  memory_kb: number | null;
  disk: { path: string | null; total_kb: number | null; free_kb: number | null };
  home: string | null;
  shell: string | null;
  sudo: "passwordless" | "password" | "none";
  package_manager: string | null;
  gnu_find: boolean;
  tools: Record<string, MachineTool>;
  gpus: MachineGpu[];
  missing_required: string[];
  complete: boolean;
  work_dir?: string;
  error?: string;
}

export interface MachineInfo {
  name: string;
  kind: "machine" | "bastion";
  source: "server" | "config" | "ui";
  editable: boolean;
  shadowed?: boolean;
  label?: string;
  ssh: string | null;
  port?: number | null;
  user?: string | null;
  identity_file?: string | null;
  via: string[];
  options?: Record<string, string>;
  ssh_args?: string[];
  work_dir?: string;
  code_server?: string;
  opencode?: string;
  codex?: string;
  strict_host_keys?: boolean;
  status: string;
  check?: MachineCheck | null;
  checked_at?: string | null;
  capabilities: MachineCaps | null;
  probed_at: string | null;
  ssh_command?: string | null;
  used_by: { workspaces: string[]; machines: string[] };
  env?: MachineEnv;
}

/** What a machine gets from this server (0600 files on it, never on a command line). */
export interface MachineEnv { forward_env: boolean; env_vars: string[]; forward_proxy: boolean; ca_bundle: string | null }

export interface BootstrapTool { name: string; for: string; installed: boolean; path: string | null; version: string | null; managed: boolean; default: boolean; configured: string }

export interface BootstrapPlan {
  probed: boolean;
  tools: BootstrapTool[];
  release_base: string;
  system_packages: { missing: string[]; package_manager: string | null; sudo: string | null; can_install: boolean; command: string | null };
  agent_settings: { source: string | null };
  running: string | null;
}

export interface Recipe {
  id: string;
  name: string;
  run: string;
  check: string | null;
  env_out: string | null;
  version: string;
  source_path: string | null;
  machines: string[];
  created_at: string | null;
  updated_at: string | null;
}

export interface MachineRecipe extends Recipe {
  position: number;
  status: "not_run" | "ok" | "failed" | "outdated";
  version_ok: string | null;
  env_vars: string[];
  error: string | null;
  last_job_id: string | null;
  last_run_at: string | null;
}

export interface MachineJob {
  id: string;
  machine: string;
  kind: string;
  title: string;
  status: "running" | "passed" | "failed" | "cancelled" | "interrupted";
  error: string | null;
  params: Record<string, unknown>;
  result: Record<string, any> | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface HostKeyHop {
  name: string;
  hostname?: string;
  port?: number;
  known_name?: string;
  scanned?: string;
  status: "trusted" | "changed" | "unknown" | "scan_failed";
  keys: { type: string; fingerprint: string; state: "trusted" | "changed" | "new"; files: string[] }[];
  can_trust: boolean;
  error?: string;
}

export interface SshKeyInfo {
  public_key_file: string;
  identity_file: string | null;
  type: string;
  fingerprint: string;
  comment: string;
  public_key: string;
}

export interface WorkspaceInfo {
  id: string;
  name: string;
  root: string;
  primary: boolean;
  configured: boolean;
  available: boolean;
  git: GitSummary | null;
  watcher: { ready: boolean; error: string | null; registering_seconds: number | null } | null;
}

export interface Bootstrap {
  workspace: { id: string; root: string; name: string; git: GitSummary };
  workspaces: WorkspaceInfo[];
  default_provider: string | null;
  providers: Record<string, Capabilities>;
  terminals: TerminalInfo[];
  named_workspaces?: NamedWorkspaceList;
  /** who is signed in: "token" or "github:<login>" (null for logins from before this was recorded) */
  identity?: string | null;
  office_info?: { available: boolean; install_hint: string | null; max_bytes: number };
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
  /** dir | markdown | image | pdf | slides | word | notebook | code | shell | data | text | archive | binary | file | broken_link */
  kind: string;
  size?: number | null;
  openable?: boolean;
  git_status: string | null;
  symlink?: boolean;
  link_target?: string | null;
  escapes?: boolean;
  format?: string;
  locked?: boolean;
  lock_inherited?: boolean;
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
  workspace_id?: string;
  workspace_name?: string | null;
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

export interface LockInfo {
  id: string;
  path: string;
  is_dir: boolean;
  reason: string | null;
  created_at: string;
}

export interface OpenDocument {
  document_id: string;
  path: string;
  /** markdown | text | docx | pdf */
  /** visio = .vsdx (text view + layout); office = layout-only (.doc, .ppt, .vsd, OpenDocument, RTF) */
  format: "markdown" | "text" | "docx" | "pptx" | "visio" | "office" | "pdf" | "image";
  /** text formats (Markdown, text/source) can be edited in the browser */
  editable?: boolean;
  /** server-rendered HTML for text/docx (markdown renders client-side; pdf has none) */
  html: string | null;
  lock: LockInfo | null;
  text: string;
  sha256: string;
  git: GitSummary;
  reading_state: ReadingState | null;
  default_session_id: string | null;
  missing: boolean;
  chat_scope?: "wiki" | "control_center";
  chat_sources?: string[];
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
  thread_id?: string;
  images?: string[]; // pictures pasted into the composer with this message (names under /threads/<id>/attachments/)
}

export interface Thread {
  id: string;
  document_id: string;
  original_anchor: Anchor;
  anchor: Anchor;
  anchor_state: "attached" | "moved" | "ambiguous" | "orphaned" | "document" | "page";
  /** page / slide / area / picture of a laid-out document, with stored images */
  /** (folder chats) kind "side": a side question about a part of an answer of the chat's conversation `of` */
  target?: { kind: "text" | "page" | "area" | "image" | "side"; page: number | null; unit: "page" | "slide"; rect?: number[] | null; label?: string | null; attachments?: string[]; of?: string; message_id?: string | null } | null;
  diff?: { base: string; target: string; side: string; line_no: number | null; line_end?: number | null; line_text: string; anchored: boolean } | null;
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
  model: string | null;
  variant: string | null;
  short_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ModelList {
  provider: string;
  models: { id: string; label: string; default?: boolean }[];
  variants: string[];
  can_select_model: boolean;
  /** what the provider uses when the session sets no model (from the provider's own config); null when unknown */
  default_model: string | null;
  default_variant: string | null;
  note: string | null;
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

// One shared EventSource for every subscriber (browsers allow only a few connections per host over HTTP/1.1).
const globalListeners = new Set<(ev: Record<string, any>) => void>();
let globalSource: EventSource | null = null;
const GLOBAL_TYPES = ["github_cache", "file_changed", "tree_changed", "job_event", "terminal_state", "preview_update", "watcher_ready", "workspaces_changed",
  "review_finished", "commit", "dev_changed", "dev_task", "machine_changed", "machine_job", "recipes_changed", "bench_run", "bench_defs_changed", "message"];

export function subscribeGlobal(onEvent: (ev: Record<string, any>) => void): () => void {
  globalListeners.add(onEvent);
  if (!globalSource) {
    const es = new EventSource(`${BASE}/events`);
    const handler = (e: MessageEvent) => {
      let ev: Record<string, any>;
      try {
        ev = JSON.parse(e.data);
      } catch {
        return;
      }
      for (const l of [...globalListeners]) {
        try {
          l(ev);
        } catch {
          /* one listener's error must not stop the others */
        }
      }
    };
    for (const t of GLOBAL_TYPES) es.addEventListener(t, handler as EventListener);
    globalSource = es;
  }
  return () => {
    globalListeners.delete(onEvent);
    if (globalListeners.size === 0 && globalSource) {
      globalSource.close();
      globalSource = null;
    }
  };
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
  commentable?: boolean;
  lock?: LockInfo | null;
  viewed?: boolean;
  viewed_stale?: boolean;
}

export interface DiffComment {
  thread_id: string;
  diff: { base: string; target: string; side: string; line_no: number | null; line_end?: number | null; line_text: string; anchored: boolean };
  question: string;
  last_status: string | null;
  message_count: number;
  last_session_id?: string | null;
  messages?: { id: string; role: string; content: string; status: string; job_id: string | null; created_at: string | null; thread_id?: string; images?: string[] }[];
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
  viewed_count?: number;
}

export interface GitRefs {
  commits: { sha: string; short: string; subject: string; date: string }[];
  branches: string[];
}

// ---------------------------------------------------------------- commit workflow

export interface GitStatus {
  branch: string | null;
  head: string | null;
  upstream: string | null;
  ahead: number | null;
  behind: number | null;
  origin: string | null;
  origin_is_github: boolean;
  gh_available: boolean;
  identity: { name: string; email: string; configured: boolean };
  files: { path: string; status: string; staged: boolean; unstaged: boolean; is_markdown: boolean; lock: LockInfo | null }[];
  recent_commits: { short: string; subject: string; when: string; author: string }[];
}

// ---------------------------------------------------------------- reviews (stash comments, submit once)

export interface ReviewComment {
  id: string;
  path: string;
  side: string;
  line_no: number | null;
  line_end: number | null;
  line_text: string;
  body: string;
  thread_id: string | null;
  decision: "address" | "reply" | "skip" | null;
  response: string | null;
  created_at: string;
}

export interface Review {
  id: string | null;
  base: string;
  target: string;
  status: "pending" | "submitted" | "completed" | "failed" | "cancelled";
  summary?: string | null;
  job_id?: string | null;
  job_status?: string | null;
  job_error?: string | null;
  result?: { summary?: string; comments?: unknown[]; parsed?: boolean; raw?: string } | null;
  comments: ReviewComment[];
  submitted_at?: string | null;
  finished_at?: string | null;
}

// ---------------------------------------------------------------- PR Development

export interface DevSettings {
  editor?: "web" | "vscode";
  python_lsp?: string;
  languages?: string[];
  python?: string;
  compile_commands_dir?: string;
  clangd?: string;
  build_command?: string;
  test_command?: string;
  debug_program?: string;
  debug_args?: string[];
}

export interface DevEditorStatus {
  state: "stopped" | "starting" | "running" | "failed";
  url: string | null;
  error: string | null;
  started_at?: number;
}

export interface DevWorkspace {
  id: string;
  name: string;
  host: string;
  source_repo: string;
  worktree_path: string;
  /** folder: works in the given checkout / folder itself (no worktree of its own) */
  mode: "branch" | "pr" | "folder";
  branch: string;
  base_ref: string | null;
  github_repo: string | null;
  pr_number: number | null;
  push_remote: string | null;
  push_branch: string | null;
  wiki_workspace_id: string | null;
  document_id: string | null;
  document_path: string;
  settings: DevSettings;
  status: "creating" | "ready" | "failed" | "archived";
  error: string | null;
  editor: DevEditorStatus;
  running_tasks: number;
  created_at: string;
  updated_at: string;
}

export interface DevInfo {
  editor: { code_server: string | null; available: boolean; extension_version: string; install_hint: string | null };
  hosts: { name: string; label: string; ssh: string | null }[];
  completion: { enabled: boolean; model: string | null; note: string | null };
  default_extensions: string[];
  kinds: string[];
}

export interface DevGitStatus {
  branch: string;
  head: string;
  subject: string;
  files: { path: string; status: string }[];
  commits_on_branch: number | null;
  unpushed: number | null;
  push_remote: string | null;
  push_branch: string | null;
  identity: { name: string; email: string; configured: boolean };
}

/** One changed file of the Changes view (PR Development). */
export interface DevDiffFile {
  path: string;
  previous_path: string | null;
  status: "modified" | "added" | "deleted" | "renamed" | "untracked";
  additions: number;
  deletions: number;
  binary: boolean;
  too_large?: boolean;
  unified: string;
}
export interface DevCompare {
  base: string;
  base_sha: string;
  target: string;
  target_sha: string | null;
  /** the target is the worktree: uncommitted files are included, and can be committed */
  worktree: boolean;
  files: DevDiffFile[];
  summary: { files: number; additions: number; deletions: number };
}
export interface DevCommitRef { sha: string; short: string; subject: string; author: string; date: string }
export interface DevRefs { branch_commits: DevCommitRef[]; commits: DevCommitRef[]; branches: string[]; base_ref: string | null; base_label: string | null }

export interface DevTaskRun {
  id: string;
  dev_id: string;
  kind: "build" | "test" | "custom" | "bench";
  command: string;
  status: "running" | "passed" | "failed" | "cancelled" | "interrupted";
  exit_code: number | null;
  started_at: string;
  finished_at: string | null;
}

export interface BenchMetric { name: string; json: string | null; regex: string | null; better: "lower" | "higher"; unit: string | null }
export interface BenchSpec {
  name: string; setup: string | null; command: string; warmup: number; repeat: number; timeout: number;
  metrics: BenchMetric[]; env: Record<string, string>; exclusive: boolean; gpus: number[]; threshold: number;
}
export interface BenchDef { name: string; source: "file" | "workspace" | "repo"; spec: BenchSpec; id: string | null; shadowed: boolean; toml: string }
export interface BenchStat { n: number; median?: number; mean?: number; stdev?: number; min?: number; max?: number; better: "lower" | "higher"; unit: string | null }
export interface BenchRun {
  id: string; dev_id: string; repo: string | null; name: string; source: "file" | "app"; machine: string; kind: "run" | "ab";
  variant: "a" | "b" | null; ab_id: string | null; label: string | null; branch: string | null; commit: string | null; dirty: boolean;
  env_hash: string | null; status: "running" | "passed" | "failed" | "cancelled" | "interrupted"; error: string | null;
  task_run_id: string | null; baseline_id: string | null; summary: Record<string, BenchStat>; started_at: string; finished_at: string | null;
  spec?: BenchSpec; samples?: { i: number; rc: number | null; values: Record<string, number | null> }[];
}
export interface BenchCompareRow { name: string; better: string; unit: string | null; a: BenchStat | null; b: BenchStat; change_pct: number | null; limit_pct?: number; verdict: "same" | "regression" | "improvement" | "missing" }
export interface BenchCompare { a: BenchRun | null; b: BenchRun; metrics: BenchCompareRow[]; warnings: string[]; regressions: number; threshold?: number }
