// Languages of the web editor beyond Monaco's built-in grammars, the file → language mapping, and an outline
// (document symbols) for languages without a language server.
import monaco from "./monaco";
import type * as M from "monaco-editor";
// Starlark (Bazel BUILD / .bzl / WORKSPACE / MODULE.bazel) is Python syntax: reuse its grammar
import * as py from "monaco-esm/languages/definitions/python/python.js";
import * as cpp from "monaco-esm/languages/definitions/cpp/cpp.js";

let registered = false;

const json: M.languages.IMonarchLanguage = {
  defaultToken: "",
  tokenPostfix: ".json",
  tokenizer: {
    root: [
      [/\/\/.*$/, "comment"],
      [/\/\*/, "comment", "@comment"],
      [/"(?:[^"\\]|\\.)*"(?=\s*:)/, "type"],
      [/"(?:[^"\\]|\\.)*"/, "string"],
      [/-?\d+(\.\d+)?([eE][-+]?\d+)?/, "number"],
      [/\b(true|false|null)\b/, "keyword"],
      [/[{}[\],:]/, "delimiter"],
    ],
    comment: [[/[^*/]+/, "comment"], [/\*\//, "comment", "@pop"], [/[*/]/, "comment"]],
  },
};

const toml: M.languages.IMonarchLanguage = {
  defaultToken: "",
  tokenPostfix: ".toml",
  tokenizer: {
    root: [
      [/#.*$/, "comment"],
      [/^\s*\[\[?[^\]]+\]\]?/, "type"],
      [/^\s*[\w.-]+(?=\s*=)/, "variable"],
      [/"""/, "string", "@mstring"],
      [/'''/, "string", "@mlstring"],
      [/"(?:[^"\\]|\\.)*"/, "string"],
      [/'[^']*'/, "string"],
      [/\d{4}-\d{2}-\d{2}([T ][\d:.]+(Z|[+-]\d{2}:\d{2})?)?/, "number"],
      [/[+-]?(\d[\d_]*)(\.\d+)?([eE][-+]?\d+)?/, "number"],
      [/\b(true|false|inf|nan)\b/, "keyword"],
    ],
    mstring: [[/"""/, "string", "@pop"], [/./, "string"]],
    mlstring: [[/'''/, "string", "@pop"], [/./, "string"]],
  },
};

const cmake: M.languages.IMonarchLanguage = {
  defaultToken: "",
  ignoreCase: true,
  tokenPostfix: ".cmake",
  keywords: ["if", "elseif", "else", "endif", "foreach", "endforeach", "while", "endwhile", "function", "endfunction", "macro",
    "endmacro", "return", "break", "continue", "block", "endblock"],
  operators: ["AND", "OR", "NOT", "STREQUAL", "EQUAL", "LESS", "GREATER", "MATCHES", "DEFINED", "EXISTS", "TARGET", "VERSION_LESS",
    "VERSION_GREATER", "VERSION_EQUAL", "IN_LIST", "PUBLIC", "PRIVATE", "INTERFACE", "REQUIRED", "COMPONENTS", "CACHE", "FORCE", "PARENT_SCOPE"],
  tokenizer: {
    root: [
      [/#\[\[/, "comment", "@bcomment"],
      [/#.*$/, "comment"],
      [/\$\{[^}]*\}/, "variable"],
      [/\$<[^>]*>/, "variable"],
      [/"/, "string", "@string"],
      [/\b[A-Za-z_]\w*(?=\s*\()/, { cases: { "@keywords": "keyword", "@default": "type" } }],
      [/\b[A-Z_][A-Z0-9_]+\b/, { cases: { "@operators": "keyword", "@default": "constant" } }],
      [/\d+(\.\d+)*/, "number"],
    ],
    string: [[/\$\{[^}]*\}/, "variable"], [/[^"\\$]+/, "string"], [/\\./, "string.escape"], [/"/, "string", "@pop"], [/\$/, "string"]],
    bcomment: [[/\]\]/, "comment", "@pop"], [/./, "comment"]],
  },
};

const makefile: M.languages.IMonarchLanguage = {
  defaultToken: "",
  tokenPostfix: ".make",
  tokenizer: {
    root: [
      [/#.*$/, "comment"],
      [/^[\w./%$(){}-]+(?=\s*::?(?!=))/, "type"],
      [/^\s*(ifeq|ifneq|ifdef|ifndef|else|endif|include|-include|define|endef|export|override|vpath)\b/, "keyword"],
      [/\$[({][^)}]*[)}]/, "variable"],
      [/\$[@<^?*+%]/, "variable"],
      [/^\s*[\w.-]+(?=\s*[:+?]?=)/, "variable"],
      [/"(?:[^"\\]|\\.)*"/, "string"],
      [/'[^']*'/, "string"],
    ],
  },
};

export function registerLanguages() {
  if (registered) return;
  registered = true;
  const L = monaco.languages;
  const add = (id: string, def: Omit<M.languages.ILanguageExtensionPoint, "id">, grammar: M.languages.IMonarchLanguage, conf?: M.languages.LanguageConfiguration) => {
    if (!L.getLanguages().some((l: M.languages.ILanguageExtensionPoint) => l.id === id)) L.register({ id, ...def });
    L.setMonarchTokensProvider(id, grammar);
    if (conf) L.setLanguageConfiguration(id, conf);
  };
  const pyMod = py as unknown as { language: M.languages.IMonarchLanguage; conf: M.languages.LanguageConfiguration };
  const cppMod = cpp as unknown as { language: M.languages.IMonarchLanguage; conf: M.languages.LanguageConfiguration };
  add("starlark", { extensions: [".bzl", ".bazel", ".star", ".sky"], filenames: ["BUILD", "WORKSPACE", "BUCK"], aliases: ["Starlark", "Bazel"] }, pyMod.language, pyMod.conf);
  add("c", { extensions: [".c"], aliases: ["C"] }, cppMod.language, cppMod.conf);
  const brackets: M.languages.LanguageConfiguration = { brackets: [["{", "}"], ["[", "]"], ["(", ")"]], autoClosingPairs: [{ open: "{", close: "}" }, { open: "[", close: "]" }, { open: "(", close: ")" }, { open: '"', close: '"' }] };
  add("json", { extensions: [".json", ".jsonc", ".json5", ".code-workspace"], filenames: [".clangd"], aliases: ["JSON"] }, json, { ...brackets, comments: { lineComment: "//", blockComment: ["/*", "*/"] } });
  add("toml", { extensions: [".toml"], filenames: ["Cargo.lock", "Pipfile", "poetry.lock"], aliases: ["TOML"] }, toml, { ...brackets, comments: { lineComment: "#" } });
  add("cmake", { extensions: [".cmake"], filenames: ["CMakeLists.txt"], aliases: ["CMake"] }, cmake, { ...brackets, comments: { lineComment: "#" } });
  add("makefile", { extensions: [".mk", ".mak"], filenames: ["Makefile", "makefile", "GNUmakefile"], aliases: ["Makefile"] }, makefile, { comments: { lineComment: "#" } });
}

const BY_NAME: Record<string, string> = {
  "cmakelists.txt": "cmake", makefile: "makefile", gnumakefile: "makefile", dockerfile: "dockerfile", containerfile: "dockerfile",
  build: "starlark", "build.bazel": "starlark", workspace: "starlark", "workspace.bazel": "starlark", "module.bazel": "starlark",
  "workspace.bzlmod": "starlark", buck: "starlark", ".bazelrc": "shell", ".bazelversion": "plaintext", "requirements.txt": "plaintext",
  ".clang-format": "yaml", ".clang-tidy": "yaml", ".pre-commit-config.yaml": "yaml", ".gitignore": "shell", ".gitattributes": "shell",
  ".editorconfig": "ini", "pipfile": "toml", "cargo.lock": "toml", ".flake8": "ini", "setup.cfg": "ini", "tox.ini": "ini", "pytest.ini": "ini",
  ".bashrc": "shell", ".zshrc": "shell", ".profile": "shell", "jenkinsfile": "java", "vagrantfile": "ruby", "gemfile": "ruby", "rakefile": "ruby",
};
const BY_EXT: Record<string, string> = {
  ".h": "cpp", ".hh": "cpp", ".hpp": "cpp", ".hxx": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp", ".cu": "cpp", ".cuh": "cpp",
  ".inl": "cpp", ".ipp": "cpp", ".tpp": "cpp", ".ino": "cpp", ".c": "c", ".m": "objective-c", ".mm": "objective-c",
  ".bzl": "starlark", ".bazel": "starlark", ".star": "starlark", ".sky": "starlark",
  ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".jsonc": "json", ".json5": "json", ".code-workspace": "json", ".ipynb": "json",
  ".toml": "toml", ".cmake": "cmake", ".mk": "makefile", ".mak": "makefile", ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ksh": "shell",
  ".py": "python", ".pyi": "python", ".pyx": "python", ".pxd": "python", ".proto": "protobuf", ".cfg": "ini", ".ini": "ini", ".conf": "ini",
  ".md": "markdown", ".markdown": "markdown", ".mdx": "mdx", ".rst": "restructuredtext", ".txt": "plaintext", ".td": "cpp", ".def": "cpp", ".ll": "plaintext",
};

export function langFor(path: string): string {
  const name = (path.split("/").pop() ?? "").toLowerCase();
  if (BY_NAME[name]) return BY_NAME[name];
  if (/^dockerfile\./.test(name) || name.endsWith(".dockerfile")) return "dockerfile";
  if (/^build\.[\w.-]+$/.test(name) && !name.includes(".md")) return "starlark"; // BUILD.foo
  if (/requirements.*\.txt$/.test(name)) return "plaintext";
  const ext = name.includes(".") ? "." + name.split(".").pop() : "";
  if (BY_EXT[ext]) return BY_EXT[ext];
  for (const l of monaco.languages.getLanguages()) if (ext && l.extensions?.includes(ext)) return l.id;
  return "plaintext";
}

// ---------------------------------------------------------------- outline without a language server

type Sym = { name: string; detail?: string; kind: number; line: number; col: number; endLine?: number; depth: number };
const K = () => monaco.languages.SymbolKind;

function lineSymbols(lang: string, text: string): Sym[] {
  const lines = text.split("\n");
  const out: Sym[] = [];
  const k = K();
  const push = (name: string, kind: number, i: number, line: string, depth = 0, detail?: string) =>
    out.push({ name, kind, line: i + 1, col: Math.max(1, line.indexOf(name) + 1), depth, detail });
  lines.forEach((line, i) => {
    let m: RegExpMatchArray | null;
    switch (lang) {
      case "markdown":
      case "mdx":
        if ((m = line.match(/^(#{1,6})\s+(.+?)\s*#*$/))) push(m[2], k.String, i, line, m[1].length - 1);
        break;
      case "yaml": {
        if ((m = line.match(/^(\s*)(?:-\s+)?([\w.$/-]+|"[^"]+"|'[^']+')\s*:(\s|$)/)) && !line.trimStart().startsWith("#")) {
          const depth = Math.floor(m[1].length / 2);
          if (depth <= 3) push(m[2].replace(/^["']|["']$/g, ""), depth === 0 ? k.Module : k.Key, i, line, depth);
        }
        break;
      }
      case "starlark":
      case "python":
        if ((m = line.match(/^(\s*)(?:async\s+)?def\s+(\w+)/))) push(m[2], m[1] ? k.Method : k.Function, i, line, m[1].length ? 1 : 0);
        else if ((m = line.match(/^(\s*)class\s+(\w+)/))) push(m[2], k.Class, i, line, m[1].length ? 1 : 0);
        else if (lang === "starlark" && (m = line.match(/^(\w+)\s*=\s*(rule|macro|provider|repository_rule|aspect|module_extension|tag_class)\(/))) push(m[1], k.Function, i, line, 0, m[2]);
        else if (lang === "starlark" && (m = line.match(/^(\w+)\($/))) {
          // a target: rule( … name = "x" …
          for (let j = i + 1; j < Math.min(lines.length, i + 40); j++) {
            const n = lines[j].match(/^\s*name\s*=\s*["']([^"']+)["']/);
            if (n) { push(n[1], k.Object, i, line, 0, m[1]); break; }
            if (/^\)/.test(lines[j])) break;
          }
        } else if (lang === "starlark" && (m = line.match(/^(\w+)\(\s*name\s*=\s*["']([^"']+)["']/))) push(m[2], k.Object, i, line, 0, m[1]);
        else if ((m = line.match(/^([A-Z_][A-Z0-9_]*)\s*=/))) push(m[1], k.Constant, i, line, 0);
        break;
      case "cmake":
        if ((m = line.match(/^\s*(function|macro)\s*\(\s*([\w-]+)/i))) push(m[2], k.Function, i, line, 0, m[1].toLowerCase());
        else if ((m = line.match(/^\s*add_(library|executable|custom_target|test)\s*\(\s*(?:NAME\s+)?([\w.:-]+)/i))) push(m[2], k.Object, i, line, 0, m[1].toLowerCase());
        else if ((m = line.match(/^\s*project\s*\(\s*([\w-]+)/i))) push(m[1], k.Package, i, line, 0, "project");
        else if ((m = line.match(/^\s*option\s*\(\s*([\w-]+)/i))) push(m[1], k.Boolean, i, line, 0, "option");
        break;
      case "toml":
      case "ini":
        if ((m = line.match(/^\s*\[\[?\s*([^\]]+?)\s*\]\]?/))) push(m[1], k.Module, i, line, 0);
        break;
      case "shell":
        if ((m = line.match(/^\s*(?:function\s+)?([\w.:-]+)\s*\(\)\s*\{?/)) || (m = line.match(/^\s*function\s+([\w.:-]+)/))) push(m[1], k.Function, i, line, 0);
        break;
      case "makefile":
        if ((m = line.match(/^([\w./%$(){}-]+)\s*::?(?!=)/)) && !line.startsWith("\t")) push(m[1], k.Function, i, line, 0);
        break;
      case "dockerfile":
        if ((m = line.match(/^\s*FROM\s+(\S+)(?:\s+AS\s+(\S+))?/i))) push(m[2] ?? m[1], k.Module, i, line, 0, m[2] ? m[1] : "FROM");
        break;
      case "protobuf":
        if ((m = line.match(/^\s*(message|enum|service)\s+(\w+)/))) push(m[2], m[1] === "service" ? k.Interface : m[1] === "enum" ? k.Enum : k.Struct, i, line, 0);
        else if ((m = line.match(/^\s*rpc\s+(\w+)/))) push(m[1], k.Method, i, line, 1);
        break;
      case "json":
        if ((m = line.match(/^(\s*)"([^"]+)"\s*:/))) {
          const depth = Math.floor(m[1].length / 2) - 1;
          if (depth >= 0 && depth <= 2) push(m[2], k.Key, i, line, depth);
        }
        break;
      case "cpp":
      case "c":
        if ((m = line.match(/^\s*(?:template\s*<.*>\s*)?(class|struct|union|enum(?:\s+class)?|namespace)\s+(\w+)/)) && !line.trim().endsWith(";")) push(m[2], m[1] === "namespace" ? k.Namespace : m[1].startsWith("enum") ? k.Enum : k.Class, i, line, 0);
        else if ((m = line.match(/^[\w:<>,*&\s]+?[\s*&]([A-Za-z_][\w:~]*)\s*\([^;]*$/)) && !/^\s*(if|for|while|switch|return|else|do)\b/.test(line)) push(m[1], k.Function, i, line, 0);
        else if ((m = line.match(/^#\s*define\s+(\w+)/))) push(m[1], k.Constant, i, line, 0);
        break;
      default:
        if ((m = line.match(/^\s*(?:export\s+)?(?:async\s+)?(?:function|fn|func|def|class|struct|interface|enum|type|trait|module|sub|proc)\s+([A-Za-z_]\w*)/))) push(m[1], k.Function, i, line, 0);
    }
  });
  return out;
}

/** Nested Monaco document symbols (by depth) for languages without a language server. */
export function outline(lang: string, model: M.editor.ITextModel): M.languages.DocumentSymbol[] {
  const syms = lineSymbols(lang, model.getValue());
  const lineCount = model.getLineCount();
  const roots: M.languages.DocumentSymbol[] = [];
  const stack: { depth: number; sym: M.languages.DocumentSymbol }[] = [];
  syms.forEach((s, idx) => {
    // a symbol spans until the next one at the same or a lower depth
    let end = lineCount;
    for (let j = idx + 1; j < syms.length; j++) if (syms[j].depth <= s.depth) { end = syms[j].line - 1; break; }
    end = Math.max(s.line, end);
    const sym: M.languages.DocumentSymbol = {
      name: s.name, detail: s.detail ?? "", kind: s.kind, tags: [],
      range: { startLineNumber: s.line, startColumn: 1, endLineNumber: end, endColumn: model.getLineMaxColumn(end) },
      selectionRange: { startLineNumber: s.line, startColumn: s.col, endLineNumber: s.line, endColumn: s.col + s.name.length },
      children: [],
    };
    while (stack.length && stack[stack.length - 1].depth >= s.depth) stack.pop();
    if (stack.length) stack[stack.length - 1].sym.children!.push(sym);
    else roots.push(sym);
    stack.push({ depth: s.depth, sym });
  });
  return roots;
}
