// A small Language Server Protocol client for Monaco. The language server runs on the development host; the backend
// bridges it over a WebSocket (one message = one LSP JSON-RPC message). This client keeps documents in sync and
// turns LSP results into Monaco providers: completion, hover, signature help, definition / declaration / type
// definition / implementation, references, highlights, outline, rename, formatting, code actions, diagnostics.
import type { Monaco } from "./monaco";
import type * as M from "monaco-editor";
import { getCsrf } from "../../api";

export type LspState = "connecting" | "ready" | "unavailable" | "exited" | "error";
export interface LspStatus {
  state: LspState;
  message?: string;
  command?: string;
  progress?: string;
}

/** Monaco language ids served by each language server. */
export const LSP_LANGS: Record<string, string[]> = {
  cpp: ["c", "cpp", "objective-c"], python: ["python"], yaml: ["yaml"], starlark: ["starlark"], shell: ["shell"], cmake: ["cmake"],
  json: ["json"], toml: ["toml"], dockerfile: ["dockerfile"], markdown: ["markdown"], go: ["go"], rust: ["rust"],
  typescript: ["typescript", "javascript"], protobuf: ["protobuf"],
};
/** Servers whose absence is worth a visible hint (the others fall back to basic navigation quietly). */
export const CORE_LSP = new Set(["cpp", "python"]);
/** LSP languageId for a Monaco language id where they differ. */
const LSP_ID: Record<string, string> = { shell: "shellscript", "objective-c": "objective-c", starlark: "starlark" };

type Json = any;

const SEVERITY = (m: Monaco, s?: number) =>
  s === 1 ? m.MarkerSeverity.Error : s === 2 ? m.MarkerSeverity.Warning : s === 3 ? m.MarkerSeverity.Info : m.MarkerSeverity.Hint;

export class LspClient {
  readonly lang: string;
  status: LspStatus = { state: "connecting" };
  private ws: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: any) => void }>();
  private caps: Json = {};
  private versions = new Map<string, number>();
  private listeners: M.IDisposable[] = [];
  private providers: M.IDisposable[] = [];
  private diagnostics = new Map<string, Json[]>(); // uri -> LSP diagnostics (for code actions)
  private ready: Promise<void>;
  private readyResolve!: () => void;
  private closed = false;
  private progress = new Map<string | number, string>();

  constructor(
    private m: Monaco,
    private devId: string,
    lang: string,
    private rootPath: string,
    private settings: { python?: string },
    private onStatus: (s: LspStatus) => void,
    private onDiagnostics: () => void,
    /** make sure a model exists for a file the server points to (definition in another file, rename edits) */
    private ensureModel: (uri: M.Uri) => Promise<M.editor.ITextModel | null>,
  ) {
    this.lang = lang;
    this.ready = new Promise((r) => (this.readyResolve = r));
  }

  get rootUri(): string {
    return this.m.Uri.file(this.rootPath).toString();
  }

  owns(model: M.editor.ITextModel): boolean {
    return LSP_LANGS[this.lang].includes(model.getLanguageId()) && model.uri.scheme === "file" &&
      (model.uri.path === this.rootPath || model.uri.path.startsWith(this.rootPath.replace(/\/$/, "") + "/"));
  }

  private setStatus(s: Partial<LspStatus>) {
    this.status = { ...this.status, ...s };
    this.onStatus(this.status);
  }

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/v1/dev/workspaces/${this.devId}/lsp/${this.lang}`, [`waw-csrf.${getCsrf() ?? ""}`]);
    this.ws = ws;
    ws.onmessage = (e) => this.onMessage(String(e.data));
    ws.onclose = (e) => {
      for (const p of this.pending.values()) p.reject(new Error("language server connection closed"));
      this.pending.clear();
      if (this.status.state === "connecting" || this.status.state === "ready") {
        this.setStatus({ state: e.code === 4412 ? "unavailable" : "exited", message: this.status.message ?? `connection closed (${e.code})` });
      }
      this.disposeProviders();
    };
    ws.onerror = () => this.setStatus({ state: "error", message: "could not connect to the language server" });
  }

  private onMessage(text: string) {
    let msg: Json;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    if (msg.waw) {
      if (msg.waw === "started") {
        this.setStatus({ command: msg.command });
        void this.initialize();
      } else if (msg.waw === "unavailable") this.setStatus({ state: "unavailable", message: msg.message });
      else if (msg.waw === "exited") this.setStatus({ state: "exited", message: `the language server exited (${msg.code})${msg.stderr ? `: ${msg.stderr}` : ""}` });
      return;
    }
    if (msg.id !== undefined && msg.method === undefined) {
      const p = this.pending.get(msg.id);
      if (p) {
        this.pending.delete(msg.id);
        if (msg.error) p.reject(msg.error);
        else p.resolve(msg.result);
      }
      return;
    }
    if (msg.id !== undefined) {
      void this.onServerRequest(msg.method, msg.params).then(
        (result) => this.send({ jsonrpc: "2.0", id: msg.id, result: result ?? null }),
        (err) => this.send({ jsonrpc: "2.0", id: msg.id, error: { code: -32603, message: String(err) } }),
      );
      return;
    }
    this.onNotification(msg.method, msg.params);
  }

  private send(obj: Json) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  request<T = any>(method: string, params: Json): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.send({ jsonrpc: "2.0", id, method, params });
    });
  }

  notify(method: string, params: Json) {
    this.send({ jsonrpc: "2.0", method, params });
  }

  /** A request that is dropped when Monaco cancels it. */
  private async req<T = any>(method: string, params: Json, token?: M.CancellationToken): Promise<T | null> {
    if (this.status.state !== "ready") return null;
    const id = this.nextId;
    const p = this.request<T>(method, params);
    const sub = token?.onCancellationRequested(() => this.notify("$/cancelRequest", { id }));
    try {
      return await p;
    } catch {
      return null;
    } finally {
      sub?.dispose();
    }
  }

  private async onServerRequest(method: string, params: Json): Promise<Json> {
    switch (method) {
      case "workspace/configuration":
        return (params.items ?? []).map((it: Json) => this.configuration(it.section));
      case "workspace/workspaceFolders":
        return [{ uri: this.rootUri, name: "worktree" }];
      case "workspace/applyEdit":
        return { applied: await this.applyWorkspaceEdit(params.edit) };
      case "window/workDoneProgress/create":
      case "client/registerCapability":
      case "client/unregisterCapability":
      case "window/showMessageRequest":
        return null;
      default:
        return null;
    }
  }

  private configuration(section: string | undefined): Json {
    const py = this.settings.python;
    const python = { pythonPath: py, defaultInterpreterPath: py, analysis: { autoSearchPaths: true, useLibraryCodeForTypes: true, diagnosticMode: "openFilesOnly" } };
    if (section === "python") return python;
    if (section === "python.analysis" || section === "basedpyright.analysis") return python.analysis;
    if (section === "basedpyright" || section === "pyright") return { analysis: python.analysis };
    if (!section) return { python };
    return null;
  }

  private onNotification(method: string, params: Json) {
    if (method === "textDocument/publishDiagnostics") {
      this.diagnostics.set(params.uri, params.diagnostics ?? []);
      const model = this.m.editor.getModel(this.m.Uri.parse(params.uri));
      if (model) this.applyDiagnostics(model);
      this.onDiagnostics();
    } else if (method === "$/progress") {
      const v = params.value ?? {};
      if (v.kind === "end") this.progress.delete(params.token);
      else this.progress.set(params.token, [v.title ?? this.progress.get(params.token) ?? "", v.message, v.percentage !== undefined ? `${v.percentage}%` : ""].filter(Boolean).join(" "));
      this.setStatus({ progress: [...this.progress.values()][0] });
    } else if (method === "window/showMessage" && params?.type === 1) {
      this.setStatus({ message: params.message });
    }
  }

  private applyDiagnostics(model: M.editor.ITextModel) {
    const diags = this.diagnostics.get(model.uri.toString()) ?? [];
    this.m.editor.setModelMarkers(model, `lsp-${this.lang}`, diags.map((d: Json) => ({
      severity: SEVERITY(this.m, d.severity),
      message: d.message,
      source: d.source,
      code: d.code !== undefined ? String(typeof d.code === "object" ? d.code.value : d.code) : undefined,
      startLineNumber: d.range.start.line + 1, startColumn: d.range.start.character + 1,
      endLineNumber: d.range.end.line + 1, endColumn: d.range.end.character + 1,
      tags: (d.tags ?? []).map((t: number) => (t === 1 ? this.m.MarkerTag.Unnecessary : this.m.MarkerTag.Deprecated)),
    })));
  }

  private async initialize() {
    try {
      const res = await this.request("initialize", {
        processId: null,
        clientInfo: { name: "wiki-agent-workspace web editor" },
        rootUri: this.rootUri,
        rootPath: this.rootPath,
        workspaceFolders: [{ uri: this.rootUri, name: "worktree" }],
        capabilities: {
          general: { positionEncodings: ["utf-16"] },
          workspace: { configuration: true, workspaceFolders: true, applyEdit: true, workspaceEdit: { documentChanges: true } },
          window: { workDoneProgress: true, showMessage: {} },
          textDocument: {
            synchronization: { didSave: true, dynamicRegistration: false },
            completion: {
              completionItem: { snippetSupport: true, documentationFormat: ["markdown", "plaintext"], insertReplaceSupport: true,
                labelDetailsSupport: true, resolveSupport: { properties: ["documentation", "detail", "additionalTextEdits"] } },
              contextSupport: true,
            },
            hover: { contentFormat: ["markdown", "plaintext"] },
            signatureHelp: { signatureInformation: { documentationFormat: ["markdown", "plaintext"], parameterInformation: { labelOffsetSupport: true } } },
            definition: { linkSupport: true }, declaration: { linkSupport: true }, typeDefinition: { linkSupport: true }, implementation: { linkSupport: true },
            references: {}, documentHighlight: {},
            documentSymbol: { hierarchicalDocumentSymbolSupport: true },
            codeAction: { codeActionLiteralSupport: { codeActionKind: { valueSet: ["", "quickfix", "refactor", "refactor.extract", "refactor.inline", "refactor.rewrite", "source", "source.organizeImports", "source.fixAll"] } } },
            formatting: {}, rangeFormatting: {}, rename: { prepareSupport: true },
            publishDiagnostics: { relatedInformation: true, tagSupport: { valueSet: [1, 2] } },
          },
        },
        initializationOptions: this.lang === "cpp" ? { clangdFileStatus: true } : undefined,
      });
      this.caps = res?.capabilities ?? {};
      this.notify("initialized", {});
      this.notify("workspace/didChangeConfiguration", { settings: { python: this.configuration("python") } });
      this.setStatus({ state: "ready", message: undefined });
      this.registerProviders();
      this.readyResolve();
      for (const model of this.m.editor.getModels()) if (this.owns(model)) this.open(model);
    } catch (e: any) {
      this.setStatus({ state: "error", message: `initialize failed: ${e?.message ?? e}` });
    }
  }

  // ---------------------------------------------------------------- documents
  private syncKind(): number {
    const s = this.caps.textDocumentSync;
    return typeof s === "number" ? s : s?.change ?? 1;
  }

  open(model: M.editor.ITextModel) {
    if (this.status.state !== "ready" || !this.owns(model) || this.versions.has(model.uri.toString())) return;
    const uri = model.uri.toString();
    this.versions.set(uri, model.getVersionId());
    this.notify("textDocument/didOpen", { textDocument: { uri, languageId: LSP_ID[model.getLanguageId()] ?? model.getLanguageId(), version: model.getVersionId(), text: model.getValue() } });
    this.listeners.push(model.onDidChangeContent((e) => {
      if (!this.versions.has(uri)) return;
      this.versions.set(uri, model.getVersionId());
      const contentChanges = this.syncKind() === 2
        ? e.changes.map((c) => ({ range: { start: { line: c.range.startLineNumber - 1, character: c.range.startColumn - 1 }, end: { line: c.range.endLineNumber - 1, character: c.range.endColumn - 1 } }, text: c.text }))
        : [{ text: model.getValue() }];
      this.notify("textDocument/didChange", { textDocument: { uri, version: model.getVersionId() }, contentChanges });
    }));
    this.listeners.push(model.onWillDispose(() => this.close(model)));
    this.applyDiagnostics(model);
  }

  saved(model: M.editor.ITextModel) {
    const uri = model.uri.toString();
    if (this.versions.has(uri)) this.notify("textDocument/didSave", { textDocument: { uri }, text: model.getValue() });
  }

  close(model: M.editor.ITextModel) {
    const uri = model.uri.toString();
    if (!this.versions.delete(uri)) return;
    this.notify("textDocument/didClose", { textDocument: { uri } });
  }

  /** All diagnostics, for the Problems view. */
  allDiagnostics(): { uri: string; diags: Json[] }[] {
    return [...this.diagnostics.entries()].filter(([, d]) => d.length).map(([uri, diags]) => ({ uri, diags }));
  }

  dispose() {
    this.closed = true;
    this.disposeProviders();
    this.listeners.forEach((l) => l.dispose());
    this.listeners = [];
    for (const model of this.m.editor.getModels()) this.m.editor.setModelMarkers(model, `lsp-${this.lang}`, []);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      void this.request("shutdown", null).catch(() => undefined).finally(() => {
        this.notify("exit", null);
        this.ws?.close();
      });
      setTimeout(() => this.ws?.close(), 1500);
    } else this.ws?.close();
  }

  private disposeProviders() {
    this.providers.forEach((p) => p.dispose());
    this.providers = [];
  }

  // ---------------------------------------------------------------- conversions
  private pos(p: M.Position) {
    return { line: p.lineNumber - 1, character: p.column - 1 };
  }

  private range(r: Json): M.IRange {
    return { startLineNumber: r.start.line + 1, startColumn: r.start.character + 1, endLineNumber: r.end.line + 1, endColumn: r.end.character + 1 };
  }

  private lspRange(r: M.IRange) {
    return { start: { line: r.startLineNumber - 1, character: r.startColumn - 1 }, end: { line: r.endLineNumber - 1, character: r.endColumn - 1 } };
  }

  private doc(model: M.editor.ITextModel, position?: M.Position) {
    return { textDocument: { uri: model.uri.toString() }, ...(position ? { position: this.pos(position) } : {}) };
  }

  private markdown(c: Json): M.IMarkdownString | undefined {
    if (!c) return undefined;
    if (typeof c === "string") return { value: c };
    if (c.kind) return { value: c.kind === "markdown" ? c.value : "```\n" + c.value + "\n```" };
    if (c.language) return { value: "```" + c.language + "\n" + c.value + "\n```" };
    return undefined;
  }

  private async locations(res: Json): Promise<M.languages.Location[]> {
    if (!res) return [];
    const list = Array.isArray(res) ? res : [res];
    const out: M.languages.Location[] = [];
    for (const l of list) {
      const uri = this.m.Uri.parse(l.targetUri ?? l.uri);
      await this.ensureModel(uri); // the peek view and "go to" need the target's text
      out.push({ uri, range: this.range(l.targetSelectionRange ?? l.range) });
    }
    return out;
  }

  private async workspaceEdit(edit: Json): Promise<M.languages.WorkspaceEdit> {
    const edits: M.languages.IWorkspaceTextEdit[] = [];
    const push = async (uriStr: string, list: Json[]) => {
      const uri = this.m.Uri.parse(uriStr);
      const model = await this.ensureModel(uri);
      for (const e of list) edits.push({ resource: uri, textEdit: { range: this.range(e.range), text: e.newText }, versionId: model?.getVersionId() });
    };
    for (const [uri, list] of Object.entries(edit?.changes ?? {})) await push(uri, list as Json[]);
    for (const dc of edit?.documentChanges ?? []) if (dc.textDocument && dc.edits) await push(dc.textDocument.uri, dc.edits);
    return { edits };
  }

  private async applyWorkspaceEdit(edit: Json): Promise<boolean> {
    const we = await this.workspaceEdit(edit);
    const byModel = new Map<string, M.languages.IWorkspaceTextEdit[]>();
    for (const e of we.edits as M.languages.IWorkspaceTextEdit[]) {
      const k = e.resource.toString();
      byModel.set(k, [...(byModel.get(k) ?? []), e]);
    }
    for (const [k, list] of byModel) {
      const model = this.m.editor.getModel(this.m.Uri.parse(k));
      if (!model) return false;
      model.pushEditOperations([], list.map((e) => ({ range: e.textEdit.range, text: e.textEdit.text })), () => null);
    }
    return true;
  }

  private completionKind(k?: number) {
    const K = this.m.languages.CompletionItemKind;
    const map: Record<number, number> = {
      1: K.Text, 2: K.Method, 3: K.Function, 4: K.Constructor, 5: K.Field, 6: K.Variable, 7: K.Class, 8: K.Interface, 9: K.Module,
      10: K.Property, 11: K.Unit, 12: K.Value, 13: K.Enum, 14: K.Keyword, 15: K.Snippet, 16: K.Color, 17: K.File, 18: K.Reference,
      19: K.Folder, 20: K.EnumMember, 21: K.Constant, 22: K.Struct, 23: K.Event, 24: K.Operator, 25: K.TypeParameter,
    };
    return map[k ?? 1] ?? K.Text;
  }

  private symbol(s: Json): M.languages.DocumentSymbol {
    const range = this.range(s.range ?? s.location.range);
    return {
      name: s.name, detail: s.detail ?? "", kind: Math.max(0, (s.kind ?? 13) - 1), tags: [], range,
      selectionRange: s.selectionRange ? this.range(s.selectionRange) : range,
      children: (s.children ?? []).map((c: Json) => this.symbol(c)),
    } as M.languages.DocumentSymbol;
  }

  // ---------------------------------------------------------------- providers
  private registerProviders() {
    const m = this.m;
    const sel = LSP_LANGS[this.lang];
    const own = (model: M.editor.ITextModel) => this.owns(model) && this.versions.has(model.uri.toString());
    const c = this.caps;
    const add = (d: M.IDisposable) => this.providers.push(d);

    if (c.completionProvider) {
      add(m.languages.registerCompletionItemProvider(sel, {
        triggerCharacters: c.completionProvider.triggerCharacters ?? [],
        provideCompletionItems: async (model, position, ctx, token) => {
          if (!own(model)) return undefined;
          const res = await this.req("textDocument/completion", {
            ...this.doc(model, position),
            context: { triggerKind: ctx.triggerKind === m.languages.CompletionTriggerKind.TriggerCharacter ? 2 : ctx.triggerKind === m.languages.CompletionTriggerKind.TriggerForIncompleteCompletions ? 3 : 1, triggerCharacter: ctx.triggerCharacter },
          }, token);
          if (!res) return undefined;
          const items: Json[] = Array.isArray(res) ? res : res.items ?? [];
          const word = model.getWordUntilPosition(position);
          const dflt = { startLineNumber: position.lineNumber, endLineNumber: position.lineNumber, startColumn: word.startColumn, endColumn: position.column };
          return {
            incomplete: !Array.isArray(res) && !!res.isIncomplete,
            suggestions: items.map((it) => {
              const te = it.textEdit;
              const range = te ? (te.range ? this.range(te.range) : { insert: this.range(te.insert), replace: this.range(te.replace) }) : dflt;
              return {
                label: it.labelDetails ? { label: it.label, detail: it.labelDetails.detail, description: it.labelDetails.description } : it.label,
                kind: this.completionKind(it.kind),
                detail: it.detail,
                documentation: this.markdown(it.documentation),
                insertText: te?.newText ?? it.insertText ?? it.label,
                insertTextRules: it.insertTextFormat === 2 ? m.languages.CompletionItemInsertTextRule.InsertAsSnippet : undefined,
                range,
                sortText: it.sortText, filterText: it.filterText, preselect: it.preselect,
                additionalTextEdits: (it.additionalTextEdits ?? []).map((e: Json) => ({ range: this.range(e.range), text: e.newText })),
                commitCharacters: it.commitCharacters,
                _lsp: it,
              } as M.languages.CompletionItem;
            }),
          };
        },
        resolveCompletionItem: c.completionProvider.resolveProvider ? async (item: any, token) => {
          const r = await this.req("completionItem/resolve", item._lsp, token);
          if (!r) return item;
          return { ...item, detail: r.detail ?? item.detail, documentation: this.markdown(r.documentation) ?? item.documentation,
            additionalTextEdits: r.additionalTextEdits ? r.additionalTextEdits.map((e: Json) => ({ range: this.range(e.range), text: e.newText })) : item.additionalTextEdits };
        } : undefined,
      }));
    }
    if (c.hoverProvider) {
      add(m.languages.registerHoverProvider(sel, {
        provideHover: async (model, position, token) => {
          if (!own(model)) return undefined;
          const r = await this.req("textDocument/hover", this.doc(model, position), token);
          if (!r) return undefined;
          const contents = (Array.isArray(r.contents) ? r.contents : [r.contents]).map((x: Json) => this.markdown(x)).filter(Boolean) as M.IMarkdownString[];
          return { contents, range: r.range ? this.range(r.range) : undefined };
        },
      }));
    }
    if (c.signatureHelpProvider) {
      add(m.languages.registerSignatureHelpProvider(sel, {
        signatureHelpTriggerCharacters: c.signatureHelpProvider.triggerCharacters ?? ["(", ","],
        signatureHelpRetriggerCharacters: c.signatureHelpProvider.retriggerCharacters ?? [],
        provideSignatureHelp: async (model, position, token) => {
          if (!own(model)) return undefined;
          const r = await this.req("textDocument/signatureHelp", this.doc(model, position), token);
          if (!r || !r.signatures?.length) return undefined;
          return {
            value: {
              activeSignature: r.activeSignature ?? 0, activeParameter: r.activeParameter ?? 0,
              signatures: r.signatures.map((s: Json) => ({
                label: s.label, documentation: this.markdown(s.documentation), activeParameter: s.activeParameter,
                parameters: (s.parameters ?? []).map((p: Json) => ({ label: p.label, documentation: this.markdown(p.documentation) })),
              })),
            },
            dispose: () => undefined,
          };
        },
      }));
    }
    const loc = (cap: string, method: string, register: (s: string[], p: any) => M.IDisposable) => {
      if (!c[cap]) return;
      add(register(sel, {
        [method]: async (model: M.editor.ITextModel, position: M.Position, token: M.CancellationToken) => {
          if (!own(model)) return undefined;
          const lspMethod = { provideDefinition: "textDocument/definition", provideDeclaration: "textDocument/declaration", provideTypeDefinition: "textDocument/typeDefinition", provideImplementation: "textDocument/implementation" }[method]!;
          return this.locations(await this.req(lspMethod, this.doc(model, position), token));
        },
      }));
    };
    loc("definitionProvider", "provideDefinition", m.languages.registerDefinitionProvider);
    loc("declarationProvider", "provideDeclaration", m.languages.registerDeclarationProvider);
    loc("typeDefinitionProvider", "provideTypeDefinition", m.languages.registerTypeDefinitionProvider);
    loc("implementationProvider", "provideImplementation", m.languages.registerImplementationProvider);
    if (c.referencesProvider) {
      add(m.languages.registerReferenceProvider(sel, {
        provideReferences: async (model, position, ctx, token) => {
          if (!own(model)) return undefined;
          return this.locations(await this.req("textDocument/references", { ...this.doc(model, position), context: { includeDeclaration: ctx.includeDeclaration } }, token));
        },
      }));
    }
    if (c.documentHighlightProvider) {
      add(m.languages.registerDocumentHighlightProvider(sel, {
        provideDocumentHighlights: async (model, position, token) => {
          if (!own(model)) return undefined;
          const r: Json[] | null = await this.req("textDocument/documentHighlight", this.doc(model, position), token);
          return (r ?? []).map((h) => ({ range: this.range(h.range), kind: (h.kind ?? 1) - 1 }));
        },
      }));
    }
    if (c.documentSymbolProvider) {
      add(m.languages.registerDocumentSymbolProvider(sel, {
        provideDocumentSymbols: async (model, token) => {
          if (!own(model)) return undefined;
          const r: Json[] | null = await this.req("textDocument/documentSymbol", this.doc(model), token);
          return (r ?? []).map((s) => this.symbol(s));
        },
      }));
    }
    if (c.renameProvider) {
      add(m.languages.registerRenameProvider(sel, {
        provideRenameEdits: async (model, position, newName, token) => {
          if (!own(model)) return undefined;
          const r = await this.req("textDocument/rename", { ...this.doc(model, position), newName }, token);
          return r ? this.workspaceEdit(r) : undefined;
        },
        resolveRenameLocation: typeof c.renameProvider === "object" && c.renameProvider.prepareProvider ? async (model, position, token) => {
          const r = await this.req("textDocument/prepareRename", this.doc(model, position), token);
          if (!r) return { range: new m.Range(1, 1, 1, 1), text: "", rejectReason: "This element cannot be renamed." };
          const range = this.range(r.range ?? r);
          return { range, text: r.placeholder ?? model.getValueInRange(range) };
        } : undefined,
      }));
    }
    if (c.documentFormattingProvider) {
      add(m.languages.registerDocumentFormattingEditProvider(sel, {
        provideDocumentFormattingEdits: async (model, options, token) => {
          if (!own(model)) return undefined;
          const r: Json[] | null = await this.req("textDocument/formatting", { ...this.doc(model), options: { tabSize: options.tabSize, insertSpaces: options.insertSpaces } }, token);
          return (r ?? []).map((e) => ({ range: this.range(e.range), text: e.newText }));
        },
      }));
    }
    if (c.documentRangeFormattingProvider) {
      add(m.languages.registerDocumentRangeFormattingEditProvider(sel, {
        provideDocumentRangeFormattingEdits: async (model, range, options, token) => {
          if (!own(model)) return undefined;
          const r: Json[] | null = await this.req("textDocument/rangeFormatting", { ...this.doc(model), range: this.lspRange(range), options: { tabSize: options.tabSize, insertSpaces: options.insertSpaces } }, token);
          return (r ?? []).map((e) => ({ range: this.range(e.range), text: e.newText }));
        },
      }));
    }
    if (c.codeActionProvider) {
      const cmdId = `waw.lsp.exec.${this.lang}.${Math.random().toString(36).slice(2, 8)}`;
      add(m.editor.registerCommand(cmdId, (_acc, cmd: Json) => void this.req("workspace/executeCommand", { command: cmd.command, arguments: cmd.arguments })));
      add(m.languages.registerCodeActionProvider(sel, {
        provideCodeActions: async (model, range, ctx, token) => {
          if (!own(model)) return undefined;
          const all = this.diagnostics.get(model.uri.toString()) ?? [];
          const diags = all.filter((d) => ctx.markers.some((mk) => mk.message === d.message && mk.startLineNumber === d.range.start.line + 1));
          const r: Json[] | null = await this.req("textDocument/codeAction", { ...this.doc(model), range: this.lspRange(range), context: { diagnostics: diags, only: ctx.only ? [ctx.only] : undefined } }, token);
          const actions: M.languages.CodeAction[] = [];
          for (const a of r ?? []) {
            if (a.command && typeof a.command === "string") {
              actions.push({ title: a.title, command: { id: cmdId, title: a.title, arguments: [a] } });
              continue;
            }
            actions.push({
              title: a.title, kind: a.kind, isPreferred: a.isPreferred,
              diagnostics: (a.diagnostics ?? []).map((d: Json) => ({ ...this.range(d.range), message: d.message, severity: SEVERITY(m, d.severity) })),
              edit: a.edit ? await this.workspaceEdit(a.edit) : undefined,
              command: a.command ? { id: cmdId, title: a.command.title, arguments: [a.command] } : undefined,
            });
          }
          return { actions, dispose: () => undefined };
        },
      }));
    }
  }

  get ready_(): boolean {
    return this.status.state === "ready";
  }

  async whenReady(): Promise<void> {
    if (this.closed) return;
    await this.ready;
  }
}
