// Wiki Agent Workspace — PR Development integration (runs in code-server's extension host, next to the worktree).
// Talks to the app with the per-editor token it was started with: WAW_DEV_URL (local) or WAW_DEV_SOCKET (SSH host,
// a socket forwarded back to the app). No dependencies; plain Node http.
"use strict";
const vscode = require("vscode");
const http = require("http");
const path = require("path");

const DEV_ID = process.env.WAW_DEV_ID || "";
const TOKEN = process.env.WAW_DEV_TOKEN || "";
const BASE_URL = process.env.WAW_DEV_URL || "";
const SOCKET = process.env.WAW_DEV_SOCKET || "";

let info = null;
let status;
let output;
let lastAnswer = null;
const running = new Map(); // job id -> label

function request(method, route, body, token) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? undefined : Buffer.from(JSON.stringify(body));
    const p = `/api/v1/dev/ext/${encodeURIComponent(DEV_ID)}${route}`;
    const opts = { method, path: p, headers: { authorization: `Bearer ${TOKEN}`, accept: "application/json" } };
    if (data) { opts.headers["content-type"] = "application/json"; opts.headers["content-length"] = data.length; }
    if (SOCKET) opts.socketPath = SOCKET;
    else {
      const u = new URL(BASE_URL);
      opts.hostname = u.hostname; opts.port = u.port || 80;
    }
    const req = http.request(opts, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => {
        const text = Buffer.concat(chunks).toString("utf8");
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch (e) { /* not json */ }
        if (res.statusCode >= 400) reject(new Error((json && json.error && json.error.message) || `HTTP ${res.statusCode}`));
        else resolve(json);
      });
    });
    req.on("error", reject);
    if (token) token.onCancellationRequested(() => req.destroy(new Error("cancelled")));
    req.setTimeout(120000, () => req.destroy(new Error("timeout")));
    if (data) req.write(data);
    req.end();
  });
}

function worktreeRoot() {
  const f = vscode.workspace.workspaceFolders;
  return f && f.length ? f[0].uri.fsPath : "";
}

function relPath(uri) {
  const root = worktreeRoot();
  const p = uri.fsPath;
  if (root && (p === root || p.startsWith(root + path.sep))) return path.relative(root, p);
  return p;
}

const SEV = ["error", "warning", "info", "hint"];

function diagList(uri, range) {
  return vscode.languages.getDiagnostics(uri)
    .filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning && (!range || d.range.intersection(range)))
    .slice(0, 60)
    .map((d) => ({ file: relPath(uri), line: d.range.start.line + 1, severity: SEV[d.severity] || "error", message: d.message,
      source: d.source || (d.code && (d.code.value || d.code)) || undefined }));
}

async function gatherContext(kind) {
  const cfg = vscode.workspace.getConfiguration("waw");
  if (cfg.get("saveBeforeAsk", true)) await vscode.workspace.saveAll(false);
  const c = {};
  const ed = vscode.window.activeTextEditor;
  if (ed && ed.document.uri.scheme === "file") {
    const doc = ed.document;
    const sel = ed.selection;
    c.file = relPath(doc.uri);
    c.language = doc.languageId;
    if (!sel.isEmpty) c.selection = { start_line: sel.start.line + 1, end_line: sel.end.line + 1, text: doc.getText(sel).slice(0, 20000) };
    else c.selection = { start_line: sel.active.line + 1, end_line: sel.active.line + 1 };
    const range = sel.isEmpty ? (kind === "fix" ? undefined : doc.lineAt(sel.active.line).range) : sel;
    c.diagnostics = diagList(doc.uri, range);
  }
  if (kind === "fix" && !(c.diagnostics && c.diagnostics.length)) {
    c.diagnostics = [];
    for (const [uri] of vscode.languages.getDiagnostics()) {
      c.diagnostics.push(...diagList(uri));
      if (c.diagnostics.length >= 60) break;
    }
  }
  if (!cfg.get("saveBeforeAsk", true)) {
    c.unsaved = vscode.workspace.textDocuments.filter((d) => d.isDirty && d.uri.scheme === "file").slice(0, 5)
      .map((d) => ({ file: relPath(d.uri), text: d.getText().slice(0, 40000) }));
  }
  return c;
}

function updateStatus() {
  if (!status) return;
  if (running.size) {
    status.text = `$(sync~spin) Agent: ${[...running.values()][0]}${running.size > 1 ? ` +${running.size - 1}` : ""}`;
    status.tooltip = "The agent is working; its answer also appears in the app's agent panel";
  } else {
    status.text = "$(hubot) Agent";
    status.tooltip = info ? `${info.name} · ${info.branch}${info.completion && info.completion.enabled ? ` · inline completion: ${info.completion.model}` : ""}` : "Agent";
  }
  status.command = "waw.ask";
}

async function poll(jobId, label) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let j;
    try { j = await request("GET", `/jobs/${encodeURIComponent(jobId)}`); } catch (e) { continue; }
    if (["completed", "failed", "cancelled", "interrupted"].includes(j.status)) {
      running.delete(jobId);
      updateStatus();
      if (j.status === "completed") {
        lastAnswer = j.text || "";
        output.clear();
        output.appendLine(`# ${label}\n`);
        output.appendLine(lastAnswer);
        const pick = await vscode.window.showInformationMessage(`Agent finished: ${label}`, "Open answer", "Review changes");
        if (pick === "Open answer") await showAnswer();
        if (pick === "Review changes") await vscode.commands.executeCommand("workbench.view.scm");
      } else {
        vscode.window.showErrorMessage(`Agent ${j.status}: ${j.error || label}`);
      }
      return;
    }
  }
}

async function showAnswer() {
  if (lastAnswer === null) return vscode.window.showInformationMessage("No answer yet.");
  const doc = await vscode.workspace.openTextDocument({ language: "markdown", content: lastAnswer });
  await vscode.window.showTextDocument(doc, { preview: true, viewColumn: vscode.ViewColumn.Beside });
  try { await vscode.commands.executeCommand("markdown.showPreview", doc.uri); } catch (e) { /* preview unavailable */ }
}

const PROMPTS = {
  explain: "Explain this code.",
  fix: "Fix the problems here.",
  tests: "Write tests for this code.",
  verify: "Verify the changes of this branch against the wiki.",
  review: "Address the pull request's review comments.",
  ci: "Fix the pull request's failing CI.",
};

async function send(kind, preset, extraContext) {
  if (!DEV_ID || !TOKEN) return vscode.window.showErrorMessage("This editor was not started by Wiki Agent Workspace.");
  let text = preset;
  if (!text || kind === "ask" || kind === "implement") {
    text = await vscode.window.showInputBox({
      prompt: kind === "implement" ? "What should the agent implement? (it reads the reference wiki first)" : "Ask the agent (with the current file, selection and diagnostics)",
      value: kind === "ask" || kind === "implement" ? "" : preset, ignoreFocusOut: true,
    });
    if (!text) return;
  }
  const context = { ...(await gatherContext(kind)), ...(extraContext || {}) };
  try {
    const r = await request("POST", "/ask", { text, kind, context });
    const label = text.length > 40 ? text.slice(0, 40) + "…" : text;
    running.set(r.job_id, label);
    updateStatus();
    poll(r.job_id, label);
  } catch (e) {
    vscode.window.showErrorMessage(`Agent request failed: ${e.message}`);
  }
}

class FixWithAgent {
  provideCodeActions(document, range, context) {
    const diags = context.diagnostics.filter((d) => d.severity <= vscode.DiagnosticSeverity.Warning);
    if (!diags.length) return [];
    const a = new vscode.CodeAction("Fix with the agent", vscode.CodeActionKind.QuickFix);
    a.diagnostics = diags;
    a.command = { command: "waw.fixDiagnostics", title: "Fix with the agent", arguments: [document.uri, diags.map((d) => ({
      file: relPath(document.uri), line: d.range.start.line + 1, severity: SEV[d.severity], message: d.message, source: d.source }))] };
    return [a];
  }
}

function sleep(ms, token) {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    token.onCancellationRequested(() => { clearTimeout(t); resolve(); });
  });
}

const inline = {
  async provideInlineCompletionItems(document, position, _context, token) {
    const cfg = vscode.workspace.getConfiguration("waw.inlineCompletion");
    if (!cfg.get("enabled", true) || !info || !info.completion || !info.completion.enabled) return;
    if (!cfg.get("languages", []).includes(document.languageId)) return;
    await sleep(cfg.get("debounceMs", 300), token);
    if (token.isCancellationRequested) return;
    const start = new vscode.Position(Math.max(0, position.line - 150), 0);
    const end = document.lineAt(Math.min(document.lineCount - 1, position.line + 50)).range.end;
    const prefix = document.getText(new vscode.Range(start, position));
    const suffix = document.getText(new vscode.Range(position, end));
    try {
      const r = await request("POST", "/complete", { language: document.languageId, path: relPath(document.uri), prefix, suffix }, token);
      if (token.isCancellationRequested || !r || !r.text) return;
      return [new vscode.InlineCompletionItem(r.text, new vscode.Range(position, position))];
    } catch (e) {
      return;
    }
  },
};

async function refreshInfo() {
  try { info = await request("GET", "/info"); } catch (e) { info = null; }
  updateStatus();
}

function activate(context) {
  output = vscode.window.createOutputChannel("Agent");
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.show();
  updateStatus();
  const reg = (id, fn) => context.subscriptions.push(vscode.commands.registerCommand(id, fn));
  reg("waw.ask", () => send("ask"));
  reg("waw.explain", () => send("explain", PROMPTS.explain));
  reg("waw.fix", () => send("fix", PROMPTS.fix));
  reg("waw.tests", () => send("tests", PROMPTS.tests));
  reg("waw.implement", () => send("implement"));
  reg("waw.verify", () => send("verify", PROMPTS.verify));
  reg("waw.review", () => send("review", PROMPTS.review));
  reg("waw.ci", () => send("ci", PROMPTS.ci));
  reg("waw.showAnswer", showAnswer);
  reg("waw.fixDiagnostics", (_uri, diags) => send("fix", PROMPTS.fix, { diagnostics: diags }));
  reg("waw.toggleInline", async () => {
    const cfg = vscode.workspace.getConfiguration("waw.inlineCompletion");
    const on = !cfg.get("enabled", true);
    await cfg.update("enabled", on, vscode.ConfigurationTarget.Global);
    vscode.window.showInformationMessage(`AI inline completion ${on ? "on" : "off"}${on && !(info && info.completion && info.completion.enabled) ? " (no completion model is configured on the server)" : ""}`);
  });
  context.subscriptions.push(status, output,
    vscode.languages.registerCodeActionsProvider({ scheme: "file" }, new FixWithAgent(), { providedCodeActionKinds: [vscode.CodeActionKind.QuickFix] }),
    vscode.languages.registerInlineCompletionItemProvider({ scheme: "file" }, inline));
  if (DEV_ID && TOKEN) {
    refreshInfo();
    const t = setInterval(refreshInfo, 5 * 60 * 1000);
    context.subscriptions.push({ dispose: () => clearInterval(t) });
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
