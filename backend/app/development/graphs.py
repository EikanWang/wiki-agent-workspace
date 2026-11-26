"""Diagrams for PR Development besides the UML class diagram, for any language / project:

- **calls**: a function's callers and the functions it calls;
- **deps**: the files a file includes / imports and the files that include / import it;
- **op**: a PyTorch-style operator's path (native_functions.yaml entry → dispatch keys → kernels, derivatives.yaml,
  Inductor lowerings / decompositions), for any project that has a native_functions.yaml;
- **impact**: the workspace's changed files, the functions changed in them and what calls those functions.

Like the UML diagram, no compiler or language server: the worktree is searched (git grep) and files are read with a
small brace / indentation scanner (C, C++, CUDA, SYCL, Java, C#, Kotlin, JS/TS, Go, Rust, Swift; Python). Macros,
overloads and calls through pointers can be missed or mixed up.

Every answer has the same shape: `nodes` (id, label, sub, path, line, col, kind, test, more) laid out in columns by
`col` (negative: to the left of the subject), `edges` (from, to, label) and `notes`.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from .files import DevFiles
from .uml import IDENT, is_test

MAX_GREP = 6000
MAX_FILES_SCANNED = 250
MAX_NODES = 40
GLOBS = ["*.h", "*.hh", "*.hpp", "*.hxx", "*.cuh", "*.inl", "*.c", "*.cc", "*.cpp", "*.cxx", "*.cu", "*.sycl", "*.py", "*.pyi",
         "*.java", "*.kt", "*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cs", "*.swift", "*.go", "*.rs", "*.m", "*.mm"]
LANG_GLOBS = {"py": ["*.py", "*.pyi"], "js": ["*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs"], "go": ["*.go"], "rust": ["*.rs"],
              "java": ["*.java", "*.kt", "*.cs", "*.swift"],
              "cpp": ["*.h", "*.hh", "*.hpp", "*.hxx", "*.cuh", "*.inl", "*.c", "*.cc", "*.cpp", "*.cxx", "*.cu", "*.sycl", "*.m", "*.mm"]}
PY_BUILTINS = {"int", "str", "float", "bool", "len", "range", "list", "dict", "set", "tuple", "isinstance", "getattr", "setattr", "hasattr",
               "type", "min", "max", "sum", "any", "all", "zip", "enumerate", "sorted", "reversed", "map", "filter", "iter", "next",
               "repr", "format", "open", "id", "hash", "abs", "round", "callable", "object", "vars", "dir", "issubclass", "frozenset",
               "bytes", "slice", "property", "staticmethod", "classmethod", "ValueError", "TypeError", "RuntimeError"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "sizeof", "do", "else", "try", "case", "new", "delete", "throw",
            "alignof", "decltype", "typeof", "static_assert", "assert", "defined", "elif", "and", "or", "not", "in", "is",
            "lambda", "await", "yield", "with", "print", "super", "self", "this", "function", "typeid", "noexcept",
            "static_cast", "dynamic_cast", "reinterpret_cast", "const_cast", "co_return", "co_await", "foreach", "using",
            "template", "operator", "constexpr", "explicit", "requires", "func", "fn", "def", "match", "loop", "unless"}
_NOT_DEF_START = re.compile(r"^\s*(return|else|case|throw|new|delete|co_return|co_await|yield|await|if|while|for|switch|do|goto|"
                            r"TORCH_CHECK|AT_ASSERT|assert|EXPECT_\w+|ASSERT_\w+|#)\b")


def lang_of(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in ("py", "pyi"):
        return "py"
    if ext in ("ts", "tsx", "js", "jsx", "mjs"):
        return "js"
    if ext == "go":
        return "go"
    if ext == "rs":
        return "rust"
    if ext in ("java", "kt", "cs", "swift"):
        return "java"
    if ext in ("h", "hh", "hpp", "hxx", "cuh", "inl", "c", "cc", "cpp", "cxx", "cu", "sycl", "m", "mm"):
        return "cpp"
    return "other"


_STR = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])\'')


def _code_lines(lines: list[str], lang: str) -> list[str]:
    """The lines without comments and string contents (so braces and parentheses in them don't count)."""
    out = []
    block = False
    for ln in lines:
        if lang == "py":
            s = ln
            if block:  # inside a docstring / triple-quoted string
                end = max(s.find('"""'), s.find("'''"))
                if end < 0:
                    out.append("")
                    continue
                s = s[end + 3:]
                block = False
            s = re.sub(r'""".*?"""|\'\'\'.*?\'\'\'', '""', s)
            m = re.search(r'"""|\'\'\'', s)
            if m:
                s = s[: m.start()]
                block = True
            out.append(re.sub(r"#.*$", "", _STR.sub('""', s)))
            continue
        s = ln
        if block:
            end = s.find("*/")
            if end < 0:
                out.append("")
                continue
            s = " " * (end + 2) + s[end + 2:]
            block = False
        s = _STR.sub('""', s)
        while True:
            a = s.find("/*")
            if a < 0:
                break
            b = s.find("*/", a + 2)
            if b < 0:
                s = s[:a]
                block = True
                break
            s = s[:a] + " " * (b + 2 - a) + s[b + 2:]
        s = re.sub(r"//.*$", "", s)
        if s.lstrip().startswith("#"):
            s = ""
        out.append(s)
    return out


_C_HEADER = re.compile(r"(~?[A-Za-z_]\w*)\s*(?:<[^<>]*>)?\s*\((?:[^()]|\([^()]*\))*\)\s*"
                       r"(?:const|noexcept(?:\([^)]*\))?|override|final|volatile|mutable|&&?|throws\s+[\w.,\s]+|->\s*[^{]+|:\s*[^{]+|"
                       r"where\s+[^{]+|async|\s)*$")


def _header_name(header: str, lang: str) -> str | None:
    h = " ".join(header.split())[-600:]
    if lang == "go":
        m = re.search(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\(", h)
        return m.group(1) if m else None
    if lang == "rust":
        m = re.search(r"\bfn\s+([A-Za-z_]\w*)", h)
        return m.group(1) if m else None
    if lang == "js":
        m = (re.search(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", h)
             or re.search(r"\b([A-Za-z_$][\w$]*)\s*[:=]\s*(?:async\s*)?(?:function\b|\([^()]*\)\s*(?::\s*[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)", h))
        if m:
            return m.group(1)
    m = _C_HEADER.search(h)
    if not m:
        return None
    name = m.group(1)
    if name in KEYWORDS or (name.isupper() and len(name) > 3 and "_" in name and lang != "cpp"):
        return None
    before = h[: m.start()].rstrip()
    if before.endswith(("=", "(", ",", "return", "[", "&&", "||", "?", "!", "+", "-")):
        return None  # a call or an expression, not a definition
    return name


def func_spans(lines: list[str], lang: str) -> list[tuple[int, int, str]]:
    """(first line, last line, name) of the functions / methods of a file, 1-based."""
    if lang == "py":
        spans = []
        open_: list[tuple[int, int, str]] = []  # (indent, line, name)
        for i, ln in enumerate(lines, 1):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            ind = len(ln) - len(ln.lstrip())
            while open_ and ind <= open_[-1][0] and not ln.lstrip().startswith((")", "]", "}")):
                _d, s, n = open_.pop()
                spans.append((s, _last_code(lines, i - 1), n))
            m = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", ln)
            if m:
                open_.append((ind, i, m.group(1)))
        for _d, s, n in open_:
            spans.append((s, _last_code(lines, len(lines)), n))
        return sorted(spans)
    code = _code_lines(lines, lang)
    spans = []
    stack: list[tuple[int, str | None]] = []  # per open brace: (line, function name or None)
    header = ""
    header_line = 1
    for i, s in enumerate(code, 1):
        for ch in s:
            if ch == "{":
                name = _header_name(header, lang) if header.strip() else None
                stack.append((header_line, name))
                header = ""
            elif ch == "}":
                if stack:
                    start, name = stack.pop()
                    if name:
                        spans.append((start, i, name))
                header = ""
            elif ch == ";":
                header = ""
            else:
                if not header.strip() and not ch.isspace():
                    header_line = i
                header += ch
        header += " "
        if len(header) > 2000:
            header = header[-2000:]
    return sorted(spans)


def _last_code(lines: list[str], i: int) -> int:
    while i > 1 and not lines[i - 1].strip():
        i -= 1
    return i


def def_regex(name: str) -> str:
    """git grep -E: a line where `name` is followed by `(` (a call or a definition) or defined with def / fn / func."""
    w = re.escape(name)
    return rf"(\b{w}\s*(<[^;]*>)?\s*\(|\b(def|fn|func)\s+(\([^)]*\)\s*)?{w}\b|\b{w}\s*[:=]\s*(async\s*)?(function|\(|[A-Za-z_]+\s*=>))"


def is_def(text: str, name: str, lang: str) -> bool:
    w = re.escape(name)
    if lang == "py":
        return bool(re.match(rf"\s*(async\s+)?def\s+{w}\s*\(", text))
    if lang == "go":
        return bool(re.match(rf"\s*func\s+(\([^)]*\)\s*)?{w}\s*[\[(]", text))
    if lang == "rust":
        return bool(re.search(rf"\bfn\s+{w}\b", text))
    if lang == "js" and (re.search(rf"\bfunction\s*\*?\s*{w}\s*\(", text)
                         or re.search(rf"\b{w}\s*[:=]\s*(async\s*)?(function\b|\([^()]*\)\s*(:\s*[^=]+)?=>|\w+\s*=>)", text)):
        return True
    t = re.sub(r"//.*$", "", text).rstrip()
    if t.endswith(";") or _NOT_DEF_START.match(t) or "=" in t.split(name, 1)[0]:
        return False
    if lang == "js":
        return (bool(re.match(rf"\s*((static|async|public|private|protected|get|set|override|readonly)\s+)*{w}\s*(<[^>]*>)?\s*\([^;]*$", t))
                and not t.lstrip().startswith(f"{name}(")) or bool(re.match(rf"\s*{w}\s*\([^)]*\)\s*(:\s*[^={{;]+)?\{{\s*$", t))
    # C / C++ / Java / C#: a type (or Class::) before the name, not inside an expression
    m = re.match(rf"\s*((template\s*<.*>|\[\[[^\]]*\]\]|__\w+__\s*(\(\([^)]*\)\))?|[\w:<>,\*&\[\]~.?]+)\s+)*[\*&]*\s*((\w+\s*(<[^<>]*>)?\s*::\s*)+)?~?{w}\s*(<[^;]*>)?\s*\(", t)
    if not m:
        return False
    head = t[: m.end()]
    mm = re.search(rf"((?:\w+\s*(?:<[^<>]*>)?\s*::\s*)*)(~?){w}\s*(<[^;]*>)?\s*\($", head)
    if not mm:
        return False
    pre = head[: mm.start()].strip()
    if not pre:  # no return type: only a constructor / destructor `Class::Class(` / `Class::~Class(`
        q = re.findall(r"(\w+)\s*(?:<[^<>]*>)?\s*::", mm.group(1))
        return bool(q) and (q[-1] == name or mm.group(2) == "~")
    return bool(re.search(r"(\w|\*|&|>)$", pre))


def _node(nodes: dict[str, dict[str, Any]], key: str, **kw: Any) -> str:
    if key not in nodes:
        nodes[key] = {"id": key, **kw}
    return key


class Graphs:
    def __init__(self, files: DevFiles):
        self.f = files
        self.t = files.t
        self._cache: dict[str, list[str]] = {}
        self._spans: dict[str, list[tuple[int, int, str]]] = {}
        self._files: list[str] | None = None

    # ------------------------------------------------------------------ helpers
    async def _grep(self, root: str, args: list[str], paths: list[str] | None = None) -> list[tuple[str, int, str]]:
        rc, out, _ = await self.t.run(["git", "-C", root, "grep", "--no-color", "-n", "-I", "--untracked", *args, "--", *(paths or GLOBS)],
                                      timeout=120)
        if rc not in (0, 1):
            return []
        hits = []
        for line in out.splitlines()[:MAX_GREP]:
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                hits.append((parts[0], int(parts[1]), parts[2]))
        return hits

    async def _lines(self, root_name: str, rel: str) -> list[str]:
        if rel not in self._cache:
            try:
                _, data = await self.f.read_bytes(root_name, rel)
                self._cache[rel] = data.decode("utf-8", "replace").split("\n")
            except Exception:
                self._cache[rel] = []
        return self._cache[rel]

    async def _func_spans(self, root_name: str, rel: str) -> list[tuple[int, int, str]]:
        if rel not in self._spans:
            self._spans[rel] = func_spans(await self._lines(root_name, rel), lang_of(rel))
        return self._spans[rel]

    async def _enclosing(self, root_name: str, rel: str, line: int) -> tuple[int, int, str] | None:
        inside = [sp for sp in await self._func_spans(root_name, rel) if sp[0] <= line <= sp[1]]
        return max(inside, key=lambda sp: sp[0]) if inside else None

    async def _all_files(self, root_name: str) -> list[str]:
        if self._files is None:
            self._files = await self.f.all_files(root_name)
        return self._files

    async def find_defs(self, root: str, names: list[str], near: str | None = None, lang: str | None = None) -> dict[str, tuple[str, int]]:
        """Where each name is defined as a function (the definition nearest to `near` first), in files of `lang`."""
        names = [n for n in dict.fromkeys(names) if IDENT.match(n)]
        globs = LANG_GLOBS.get(lang or "")
        out: dict[str, tuple[str, int]] = {}
        here = (near or "").rsplit("/", 1)[0]
        for i in range(0, len(names), 40):
            chunk = names[i: i + 40]
            alt = "|".join(re.escape(n) for n in chunk)
            hits = await self._grep(root, ["-E", "-e", rf"\b({alt})\s*(<[^;]*>)?\s*\(", "-e", rf"\b(def|fn|func)\s+(\([^)]*\)\s*)?({alt})\b",
                                           "-e", rf"\b({alt})\s*[:=]\s*(async\s*)?(function|\()"], globs)
            found: dict[str, list[tuple[str, int]]] = {}
            for p, n, t in hits:
                for nm in chunk:
                    if nm in t and is_def(t, nm, lang_of(p)):
                        found.setdefault(nm, []).append((p, n))
            for nm, locs in found.items():
                locs.sort(key=lambda x: (x[0] != near, bool(is_test("", x[0])), not x[0].startswith(here) if here else False,
                                         not re.search(r"\.(c|cc|cpp|cxx|cu|py|go|rs|java|ts|js|sycl)$", x[0]), len(x[0]), x))
                out[nm] = locs[0]
        return out

    # ------------------------------------------------------------------ call graph
    async def calls(self, root_name: str, name: str, near: str | None = None, at: tuple[str, int] | None = None,
                    tests: bool = False, parts: set[str] | None = None) -> dict[str, Any]:
        """Who calls `name` and what it calls (one level each; the page expands a node for more)."""
        if not IDENT.match(name or ""):
            return {"error": "not a function name"}
        parts = parts or {"callers", "callees"}
        root = await self.f.root(root_name)
        lang = lang_of(at[0] if at else near or "")
        hits = await self._grep(root, ["-E", "-e", def_regex(name)], LANG_GLOBS.get(lang))
        defs = [(p, n) for p, n, t in hits if is_def(t, name, lang_of(p))]
        if at:
            fn = at
        elif defs:
            here = (near or "").rsplit("/", 1)[0]
            defs.sort(key=lambda x: (x[0] != near, is_test("", x[0]), not x[0].startswith(here) if here else False,
                                     not re.search(r"\.(c|cc|cpp|cxx|cu|py|go|rs|java|ts|js|sycl)$", x[0]), len(x[0]), x))
            fn = defs[0]
        else:
            fn = None
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        notes: list[str] = []
        center = _node(nodes, f"{fn[0]}:{fn[1]}" if fn else f"?:{name}", label=name, path=fn[0] if fn else None,
                       line=fn[1] if fn else None, col=0, kind="subject", sub=fn[0] if fn else "definition not found")
        if fn and len(defs) > 1:
            notes.append(f"{len(defs)} definitions of {name} (overloads or same-named functions); this one is {fn[0]}:{fn[1]}.")
        hidden: set[str] = set()
        if "callers" in parts:
            callers: dict[str, dict[str, Any]] = {}
            files = sorted({p for p, n, t in hits if not is_def(t, name, lang_of(p))})
            for p, n, t in hits:
                if p not in files[:MAX_FILES_SCANNED] or is_def(t, name, lang_of(p)):
                    continue
                if not re.search(rf"\b{re.escape(name)}\s*(<[^;]*>)?\s*\(", re.sub(r"//.*$|#.*$", "", t)):
                    continue
                enc = await self._enclosing(root_name, p, n)
                if not enc and re.search(rf"[\w*&>]\s+{re.escape(name)}\s*\(", t) and not re.search(r"=|\breturn\b", t.split(name, 1)[0]):
                    continue  # a declaration (in a header), not a call
                if fn and enc and p == fn[0] and enc[0] == fn[1]:
                    continue  # recursion
                label = enc[2] if enc else p.rsplit("/", 1)[-1]
                key = f"{p}:{enc[0] if enc else 0}"
                if not tests and is_test(label if enc else "", p):
                    hidden.add(label)
                    continue
                c = callers.setdefault(key, {"id": key, "label": label, "path": p, "line": enc[0] if enc else n, "col": -1,
                                             "kind": "function" if enc else "file", "sub": p, "count": 0, "fn": label if enc else None})
                c["count"] += 1
            clist = sorted(callers.values(), key=lambda c: (c["kind"] == "file", -c["count"], c["path"]))
            if len(clist) > MAX_NODES:
                notes.append(f"{len(clist) - MAX_NODES} more callers not shown.")
            for c in clist[:MAX_NODES]:
                nodes[c["id"]] = c
                edges.append({"from": c["id"], "to": center, "label": f"×{c['count']}" if c["count"] > 1 else ""})
            if len(files) > MAX_FILES_SCANNED:
                notes.append(f"Only the first {MAX_FILES_SCANNED} of {len(files)} files that mention {name} were read.")
        if "callees" in parts and fn:
            spans = [sp for sp in await self._func_spans(root_name, fn[0]) if sp[0] == fn[1]]
            lines = await self._lines(root_name, fn[0])
            if spans:
                body = _code_lines(lines[spans[0][0] - 1: spans[0][1]], lang_of(fn[0]))
                first = next((i for i, s in enumerate(body) if "{" in s or s.rstrip().endswith(":")), 0)
                called: dict[str, int] = {}
                for s in body[first + 1 if lang_of(fn[0]) == "py" else first:]:
                    for m in re.finditer(r"(?<![\w$])([A-Za-z_]\w*)\s*(?:<[^;()]*>)?\s*\(", s):
                        nm = m.group(1)
                        if nm not in KEYWORDS and nm != name and not nm.isupper() and not (lang_of(fn[0]) == "py" and nm in PY_BUILTINS):
                            called[nm] = called.get(nm, 0) + 1
                defs_of = await self.find_defs(root, list(called)[:120], fn[0], lang_of(fn[0]))
                for s0, _e0, nm0 in await self._func_spans(root_name, fn[0]):  # functions defined inside it win
                    if spans[0][0] < s0 <= spans[0][1] and nm0 in called:
                        defs_of[nm0] = (fn[0], s0)
                shown = 0
                for nm, cnt in called.items():
                    if nm not in defs_of:
                        continue
                    p, n = defs_of[nm]
                    if not tests and is_test(nm, p):
                        hidden.add(nm)
                        continue
                    if shown >= MAX_NODES:
                        notes.append("More called functions not shown.")
                        break
                    key = _node(nodes, f"{p}:{n}", label=nm, path=p, line=n, col=1, kind="function", sub=p, fn=nm)
                    if key == center:
                        continue
                    edges.append({"from": center, "to": key, "label": f"×{cnt}" if cnt > 1 else ""})
                    shown += 1
                ext = [nm for nm in called if nm not in defs_of]
                if ext:
                    notes.append(f"Called but not defined in this worktree (library, macro or method of another type): "
                                 f"{', '.join(ext[:15])}{'…' if len(ext) > 15 else ''}.")
            else:
                notes.append("The function's body wasn't found, so what it calls isn't shown.")
        return {"kind": "calls", "title": f"Calls of {name}", "nodes": list(nodes.values()), "edges": edges, "notes": notes,
                "hidden_tests": sorted(hidden), "center": center}

    # ------------------------------------------------------------------ includes / imports
    def _resolve(self, spec: str, kind: str, src: str, files: set[str], by_tail: dict[str, list[str]]) -> str | None:
        here = posixpath.dirname(src)
        if kind == "include":
            cand = posixpath.normpath(posixpath.join(here, spec))
            if cand in files:
                return cand
            hits = by_tail.get(spec.rsplit("/", 1)[-1], [])
            hits = [h for h in hits if h == spec or h.endswith("/" + spec)]
            return min(hits, key=len) if hits else None
        if kind in ("py", "pyname"):
            dots = len(spec) - len(spec.lstrip("."))
            mod = spec.lstrip(".").replace(".", "/")
            if dots:
                base = here
                for _ in range(dots - 1):
                    base = posixpath.dirname(base)
                stems = [posixpath.join(base, mod) if mod else base]
            else:
                stems = [mod] + [h[: -len(mod) - 3] + mod for h in by_tail.get(mod.rsplit("/", 1)[-1] + ".py", []) if h.endswith("/" + mod + ".py")]
            for st in stems:
                for c in (st + ".py", st + "/__init__.py", st + ".pyi"):
                    if c in files:
                        return c
            return None
        if kind == "js":
            if not spec.startswith("."):
                return None
            st = posixpath.normpath(posixpath.join(here, spec))
            for c in (st, *(st + e for e in (".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.tsx", "/index.js"))):
                if c in files:
                    return c
            return None
        if kind == "java":
            p = spec.replace(".", "/")
            hits = [h for h in by_tail.get(p.rsplit("/", 1)[-1] + ".java", []) + by_tail.get(p.rsplit("/", 1)[-1] + ".kt", [])
                    if h.endswith(p + ".java") or h.endswith(p + ".kt")]
            return hits[0] if hits else None
        return None

    @staticmethod
    def imports_in(line: str, lang: str) -> list[tuple[str, str]]:
        """(spec, kind) of what one line includes / imports."""
        if lang == "cpp":
            m = re.match(r'\s*#\s*(?:include|import)\s*([<"])([^>"]+)[>"]', line)
            return [(m.group(2), "include")] if m else []
        if lang == "py":
            m = re.match(r"\s*from\s+(\.*[\w.]*)\s+import\s+(.+)", line)
            if m:
                names = [x.strip().split(" as ")[0].strip("() ") for x in m.group(2).split(",")]
                mod = m.group(1)
                sep = "" if mod.endswith(".") else "."
                return [(mod, "py")] + [(mod + sep + n, "pyname") for n in names if IDENT.match(n)]
            m = re.match(r"\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)", line)
            return [(x.strip(), "py") for x in m.group(1).split(",")] if m else []
        if lang == "js":
            return [(m.group(1), "js") for m in re.finditer(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"]([^'"]+)['"]""", line)]
        if lang == "java":
            m = re.match(r"\s*import\s+(?:static\s+)?([\w.]+)\s*;?", line)
            return [(m.group(1), "java")] if m else []
        if lang in ("go", "rust"):
            m = re.match(r'\s*(?:import\s+)?(?:\w+\s+)?"([^"]+)"\s*$', line) if lang == "go" else re.match(r"\s*(?:pub\s+)?use\s+([\w:]+)", line)
            return [(m.group(1), "ext")] if m else []
        return []

    async def deps(self, root_name: str, path: str, tests: bool = False) -> dict[str, Any]:
        lang = lang_of(path)
        files_l = await self._all_files(root_name)
        files = set(files_l)
        if path not in files:
            return {"error": "file not found in the worktree"}
        by_tail: dict[str, list[str]] = {}
        for f in files_l:
            by_tail.setdefault(f.rsplit("/", 1)[-1], []).append(f)
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        notes: list[str] = []
        center = _node(nodes, path, label=path.rsplit("/", 1)[-1], path=path, line=1, col=0, kind="subject", sub=posixpath.dirname(path))
        # what it imports
        ext: list[str] = []
        seen: set[str] = set()
        for n, ln in enumerate(await self._lines(root_name, path), 1):
            for spec, kind in self.imports_in(ln, lang):
                r = self._resolve(spec, kind, path, files, by_tail)
                if r and r != path and r not in seen:
                    seen.add(r)
                    if len(seen) > MAX_NODES:
                        continue
                    k = _node(nodes, r, label=r.rsplit("/", 1)[-1], path=r, line=1, col=1, kind="file", sub=posixpath.dirname(r), at=n)
                    edges.append({"from": center, "to": k})
                elif not r and kind != "pyname" and spec not in ext:
                    ext.append(spec)
        if len(seen) > MAX_NODES:
            notes.append(f"{len(seen) - MAX_NODES} more included / imported files not shown.")
        if ext:
            notes.append(f"Outside this worktree (system, third-party or standard library): {', '.join(ext[:20])}{'…' if len(ext) > 20 else ''}.")
        # what imports it
        stem = path.rsplit("/", 1)[-1]
        mod = stem.rsplit(".", 1)[0]
        if lang == "cpp":
            pats = ["-E", "-e", rf'#\s*(include|import)\s*[<"]([^">]*/)?{re.escape(stem)}[">]']
        elif lang == "py":
            name = posixpath.basename(posixpath.dirname(path)) if mod == "__init__" else mod
            pats = ["-E", "-e", rf"^\s*(from|import)\s.*\b{re.escape(name)}\b"]
        elif lang == "js":
            pats = ["-E", "-e", rf"""(from|import|require)\s*\(?\s*['"][^'"]*\b{re.escape(mod)}(\.\w+)?['"]"""]
        elif lang == "java":
            pats = ["-E", "-e", rf"^\s*import\s.*\b{re.escape(mod)}\s*;"]
        else:
            pats = []
        hidden: set[str] = set()
        users = 0
        for p, n, t in (await self._grep(await self.f.root(root_name), pats) if pats else []):
            if p == path or (p in nodes and nodes[p]["col"] == -1):
                continue
            if not any(self._resolve(spec, kind, p, files, by_tail) == path for spec, kind in self.imports_in(t, lang_of(p))):
                continue
            if not tests and is_test("", p):
                hidden.add(p.rsplit("/", 1)[-1])
                continue
            users += 1
            if users > MAX_NODES:
                continue
            key = p if p not in nodes else f"{p}#in"
            _node(nodes, key, label=p.rsplit("/", 1)[-1], path=p, line=n, col=-1, kind="file", sub=posixpath.dirname(p))
            edges.append({"from": key, "to": center})
        if users > MAX_NODES:
            notes.append(f"{users - MAX_NODES} more files that include / import it not shown.")
        if not pats:
            notes.append("Who imports this file isn't searched for this language.")
        return {"kind": "deps", "title": f"Dependencies of {stem}", "nodes": list(nodes.values()), "edges": edges, "notes": notes,
                "hidden_tests": sorted(hidden), "center": center}

    # ------------------------------------------------------------------ operator path (native_functions.yaml projects)
    async def op(self, root_name: str, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        base = name.split(".", 1)[0]
        if not IDENT.match(base):
            return {"error": "not an operator name"}
        root = await self.f.root(root_name)
        files_l = await self._all_files(root_name)
        yamls = [f for f in files_l if f.endswith("native_functions.yaml") or re.search(r"(^|/)(xpu|native)_functions\.yaml$", f)]
        derivs = [f for f in files_l if f.endswith("derivatives.yaml")]
        if not yamls:
            return {"error": "this project has no native_functions.yaml (operator paths are for PyTorch-style projects)"}
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        notes: list[str] = []
        kernels: dict[str, list[tuple[str, str]]] = {}  # kernel → [(overload node, keys)]
        delegates: list[tuple[str, str]] = []
        keys_seen: set[str] = set()
        composite = False
        for y in yamls:
            lines = await self._lines(root_name, y)
            i = 0
            while i < len(lines):
                m = re.match(r"-\s*func:\s*([\w]+)(?:\.(\w+))?\s*\(", lines[i])
                if not m or m.group(1) not in (base, base + "_") or ("." in name and m.group(2) != name.split(".", 1)[1]):
                    i += 1
                    continue
                start = i
                i += 1
                while i < len(lines) and not lines[i].startswith("- func:"):
                    i += 1
                block = lines[start:i]
                ov = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
                sig = block[0].split("func:", 1)[1].strip()
                key = _node(nodes, f"{y}:{start + 1}", label=ov, path=y, line=start + 1, col=0, kind="subject",
                            sub=y, detail=sig[:300])
                in_dispatch = False
                for ln in block[1:]:
                    s = ln.rstrip()
                    if re.match(r"\s*dispatch:\s*$", s):
                        in_dispatch = True
                        continue
                    if in_dispatch and re.match(r"\s{4,}[\w, ]+:\s*\S", s):
                        ks, kern = s.split(":", 1)
                        ks_l = [k.strip() for k in ks.split(",")]
                        keys_seen.update(ks_l)
                        composite = composite or any(k.startswith("Composite") for k in ks_l)
                        kernels.setdefault(kern.strip(), []).append((key, ", ".join(ks_l)))
                        continue
                    in_dispatch = False
                    md = re.match(r"\s*structured_delegate:\s*([\w.]+)", s)
                    if md:
                        delegates.append((key, md.group(1)))
                if not any(re.match(r"\s*(dispatch|structured_delegate):", ln) for ln in block):
                    composite = True
                    notes.append(f"{ov} has no dispatch entry: it is CompositeImplicitAutograd (written in other ops).")
        if not nodes:
            return {"error": f"{name} isn't in {', '.join(yamls[:3])}"}
        for key, target in delegates:
            edges += [{"from": key, "to": nd["id"], "label": "structured"} for nd in nodes.values() if nd["label"] == target]
        defs = await self.find_defs(root, [k for k in kernels if IDENT.match(k)], lang="cpp")
        impl = await self._grep(root, ["-E", "-e", r"TORCH_IMPL_FUNC\s*\(\s*(" + "|".join(re.escape(k) for k in kernels if IDENT.match(k)) + r")\s*\)"]) if kernels else []
        for kern, users in kernels.items():
            loc = defs.get(kern)
            if not loc:
                hit = next(((p, n) for p, n, t in impl if re.search(rf"\(\s*{re.escape(kern)}\s*\)", t)), None)
                loc = hit
            k = _node(nodes, f"kernel:{kern}", label=kern, path=loc[0] if loc else None, line=loc[1] if loc else None, col=1,
                      kind="function" if loc else "missing", sub=loc[0] if loc else "not found in this worktree")
            for ov_key, ks in users:
                edges.append({"from": ov_key, "to": k, "label": ks})
        for d in derivs:
            for n, ln in enumerate(await self._lines(root_name, d), 1):
                m = re.match(r"-\s*name:\s*(\w+)(?:\.(\w+))?\s*\(", ln)
                if m and m.group(1) in (base, base + "_") and (("." not in name) or m.group(2) == name.split(".", 1)[1]):
                    label = m.group(1) + (f".{m.group(2)}" if m.group(2) else "")
                    k = _node(nodes, f"{d}:{n}", label=f"∂ {label}", path=d, line=n, col=-1, kind="derivative", sub=d)
                    tgt = next((nd["id"] for nd in nodes.values() if nd["kind"] == "subject" and nd["label"] == label), None)
                    if tgt:
                        edges.append({"from": k, "to": tgt, "label": "autograd"})
        # Inductor lowerings, decompositions, references, meta registrations
        hits = await self._grep(root, ["-E", "-e", rf"\baten\.{re.escape(base)}\b"], ["torch/_inductor/*.py", "torch/_decomp/*.py",
                                                                                         "torch/_refs/*.py", "torch/_meta_registrations.py",
                                                                                         "torch/_prims/*.py"])
        by_file: dict[str, int] = {}
        for p, n, t in hits:
            if re.search(r"\b(register_lowering|register_decomposition|make_fallback|register_meta|register_op_impl|"
                         r"_register_\w+|add_needs_realized_inputs|decomps?_to_exclude|fallback_\w+|lowerings?\[)|^\s*@", t):
                by_file.setdefault(p, n)
        subject = next(nd["id"] for nd in nodes.values() if nd["kind"] == "subject")
        for p, n in list(by_file.items())[:15]:
            kind = "lowering" if "_inductor" in p else "decomposition" if "_decomp" in p or "_refs" in p else "meta"
            k = _node(nodes, f"{p}:{n}", label=p.rsplit("/", 1)[-1], path=p, line=n, col=-1, kind=kind, sub=kind)
            edges.append({"from": k, "to": subject, "label": kind})
        if not composite:
            missing = [k for k in ("CPU", "CUDA", "XPU", "MPS") if k not in keys_seen and not any(k in s for s in keys_seen)]
            if missing:
                notes.append(f"No kernel registered here for: {', '.join(missing)}." + (
                    " XPU kernels may live in torch-xpu-ops (third_party/torch-xpu-ops/yaml)." if "XPU" in missing else ""))
        return {"kind": "op", "title": f"Operator {name}", "nodes": list(nodes.values()), "edges": edges, "notes": notes,
                "hidden_tests": [], "center": subject}

    # ------------------------------------------------------------------ PR impact
    async def impact(self, root_name: str, base: str, tests: bool = False) -> dict[str, Any]:
        """The changed files (since the merge base with `base`, uncommitted changes included), the functions changed in
        them and the functions that call those."""
        root = await self.f.root(root_name)
        rc, mb, _ = await self.t.git(root, ["merge-base", base, "HEAD"])
        mb = mb.strip() if rc == 0 and mb.strip() else base
        rc, diff, _ = await self.t.git(root, ["diff", "--no-color", "-U0", "--no-renames", mb], timeout=120)
        rc2, untracked, _ = await self.t.git(root, ["ls-files", "-o", "--exclude-standard"])
        changed: dict[str, set[int]] = {}
        cur = None
        for ln in diff.splitlines():
            if ln.startswith("+++ "):
                cur = None if ln[4:] == "/dev/null" else ln[6:]
                if cur:
                    changed.setdefault(cur, set())
            elif ln.startswith("--- ") and ln[4:] != "/dev/null":
                changed.setdefault(ln[6:], set())
            elif ln.startswith("@@") and cur:
                m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", ln)
                if m:
                    a, c = int(m.group(1)), int(m.group(2) or 1)
                    changed[cur].update(range(a, a + max(c, 1)))
        for u in untracked.splitlines() if rc2 == 0 else []:
            changed.setdefault(u, {0})
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        notes: list[str] = []
        if not changed:
            return {"kind": "impact", "title": "Impact of the changes", "nodes": [], "edges": [], "notes": [f"No changes since {base}."],
                    "hidden_tests": [], "center": None}
        funcs: list[tuple[str, str, int]] = []  # (file node, name, line)
        files_known = set(await self._all_files(root_name))
        for p in sorted(changed)[:60]:
            gone = p not in files_known
            test = is_test("", p)
            fk = _node(nodes, p, label=p.rsplit("/", 1)[-1], path=None if gone else p, line=1, col=0,
                       kind="deleted" if gone else "changed", sub=posixpath.dirname(p), test=test)
            if gone or lang_of(p) == "other":
                continue
            lines_ch = changed[p]
            spans = await self._func_spans(root_name, p)
            if 0 in lines_ch:  # a new file: all its functions
                hit = spans
            else:
                inner = set()
                for x in lines_ch:  # the innermost function around each changed line
                    around = [sp for sp in spans if sp[0] <= x <= sp[1]]
                    if around:
                        inner.add(max(around, key=lambda sp: sp[0]))
                hit = sorted(inner)
            for s, _e, nm in hit[:12]:
                k = _node(nodes, f"{p}:{s}", label=nm, path=p, line=s, col=1, kind="function", sub=p, test=test, fn=nm)
                edges.append({"from": fk, "to": k, "label": "changes"})
                if not test:
                    funcs.append((k, nm, s))
        if len(changed) > 60:
            notes.append(f"{len(changed) - 60} more changed files not shown.")
        # callers of the changed functions (tests apart)
        tests_using: dict[str, set[str]] = {}
        for k, nm, s in funcs[:25]:
            res = await self.calls(root_name, nm, at=(nodes[k]["path"], s), tests=True, parts={"callers"})
            shown = 0
            for c in res.get("nodes", []):
                if c.get("col") != -1:
                    continue
                if is_test(c["label"] if c["kind"] == "function" else "", c["path"]):
                    tests_using.setdefault(c["path"], set()).add(nm)
                    continue
                if c["id"] in nodes and nodes[c["id"]]["col"] in (0, 1):
                    edges.append({"from": c["id"], "to": k, "label": "calls"})
                    continue
                if shown >= 8:
                    nodes[k]["more"] = nodes[k].get("more", 0) + 1
                    continue
                shown += 1
                _node(nodes, c["id"], label=c["label"], path=c["path"], line=c["line"], col=2, kind=c["kind"], sub=c["path"], fn=c.get("fn"))
                edges.append({"from": c["id"], "to": k, "label": "calls"})
        if len(funcs) > 25:
            notes.append(f"Callers searched for the first 25 of {len(funcs)} changed functions.")
        for p, names in sorted(tests_using.items())[:20]:
            k = _node(nodes, f"test:{p}", label=p.rsplit("/", 1)[-1], path=p, line=1, col=3, kind="test", sub=p, test=True)
            for nm in sorted(names):
                tgt = next((f[0] for f in funcs if f[1] == nm), None)
                if tgt:
                    edges.append({"from": k, "to": tgt, "label": "tests"})
        if not tests_using:
            notes.append("No test calls the changed functions directly.")
        return {"kind": "impact", "title": f"Impact of the changes since {base}", "nodes": list(nodes.values()), "edges": edges,
                "notes": notes, "hidden_tests": [], "center": None}

