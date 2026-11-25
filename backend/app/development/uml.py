"""A UML class diagram for PR Development: a class with its members, its parent classes (up to a few levels), the
classes that derive from it and the classes / files that use it.

No compiler or language server: the worktree is searched (git grep) with C++ / Python / Java / TypeScript / C#
patterns and the class bodies are read with a small brace / indentation scanner. It answers "what is around this
class" well enough to read code; overloads, macros-generated classes and base lists spread over many lines can be
missed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from .files import DevFiles

MAX_GREP = 5000
MAX_MEMBERS = 30
MAX_CHILDREN = 40
MAX_USERS = 30
MAX_DEPTH = 4
PARTS = ("class", "parents", "children", "users")
IDENT = re.compile(r"^[A-Za-z_]\w{0,199}$")
SOURCE_GLOBS = ["*.h", "*.hh", "*.hpp", "*.hxx", "*.cuh", "*.inl", "*.c", "*.cc", "*.cpp", "*.cxx", "*.cu", "*.py", "*.pyi",
                "*.java", "*.kt", "*.ts", "*.tsx", "*.cs", "*.swift"]
_ACCESS = {"public", "protected", "private", "virtual"}
_PY_IGNORE = {"object", "Generic", "Protocol", "ABC"}


def _def_regex(name: str) -> str:
    """git grep -E: a class / struct definition of `name` (not a forward declaration)."""
    w = re.escape(name)
    return (rf"^\s*(export\s+)?(public\s+|private\s+|internal\s+|abstract\s+|final\s+|sealed\s+|data\s+|open\s+|static\s+|default\s+)*"
            rf"(template\s*<.*>\s*)?(class|struct|interface)\s+([A-Za-z_][A-Za-z_0-9:()]*\s+)*{w}\b")


def _split_top(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _base_name(s: str) -> str | None:
    s = re.sub(r"<.*>", "", s.strip())  # template arguments
    s = re.sub(r"\[.*\]", "", s)  # Python generics
    words = [w for w in re.split(r"\s+", s) if w and w not in _ACCESS]
    if not words:
        return None
    last = re.split(r"::|\.", words[-1])[-1]
    return last if IDENT.match(last) else None


def parse_header(text: str, name: str) -> dict[str, Any]:
    """Kind and base classes from the text of a class header (from `class Name` to its `{` or `:`)."""
    m = re.search(rf"\b(class|struct|interface)\s+(?:[\w:()]+\s+)*{re.escape(name)}\b(.*)", text, re.S)
    kind = m.group(1) if m else "class"
    rest = m.group(2) if m else ""
    bases: list[str] = []
    py = re.match(r"\s*\((.*?)\)\s*:", rest, re.S)
    if py:  # Python
        for part in _split_top(py.group(1)):
            if "=" in part:
                continue
            b = _base_name(part)
            if b and b not in _PY_IGNORE:
                bases.append(b)
        return {"kind": kind, "bases": bases}
    rest = re.sub(r"^\s*<[^{]*?>", "", rest)  # a template specialization: Name<T>
    rest = re.sub(r"^\s*final\b", "", rest)
    ext = re.match(r"\s*(?:extends\s+([^{]*?))?\s*(?:implements\s+([^{]*))?\s*\{", rest, re.S)
    if ext and (ext.group(1) or ext.group(2)):  # Java / TypeScript
        for grp in (ext.group(1), ext.group(2)):
            for part in _split_top(grp or ""):
                b = _base_name(part)
                if b:
                    bases.append(b)
        return {"kind": kind, "bases": bases}
    cpp = re.match(r"\s*:(?!:)([^{;]*)", rest, re.S)
    if cpp:  # C++ / C# / Kotlin / Swift
        for part in _split_top(cpp.group(1)):
            b = _base_name(re.sub(r"\(.*\)", "", part))
            if b:
                bases.append(b)
    return {"kind": kind, "bases": bases}


def _member(decl: str, lang: str) -> dict[str, Any] | None:
    d = " ".join(decl.split())
    if not d or d.startswith(("#", "//", "/*", "*", "@", "friend ", "using ", "typedef ", "static_assert", "C10_", "TORCH_")):
        return None
    d = re.sub(r"\s*\{.*$", "", d).rstrip(";").strip()
    if "(" in d:  # a pure virtual / defaulted / deleted method: = 0 etc. (a field keeps its initializer)
        d = re.sub(r"\s*=\s*(0|default|delete)\s*$", "", d)
    d = re.sub(r"\s+(override|final)\b.*$", "", d).strip()
    if not d:
        return None
    if len(d) > 160:
        d = d[:157] + "…"
    return {"text": d, "method": "(" in d}


def scan_members(lines: list[str], start: int, lang: str) -> list[dict[str, Any]]:
    """Members of the class whose header is at index `start` (0-based)."""
    out: list[dict[str, Any]] = []
    if lang == "py":
        indent = len(lines[start]) - len(lines[start].lstrip())
        inner: int | None = None
        for ln in lines[start + 1: start + 3000]:
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            ind = len(ln) - len(ln.lstrip())
            if ind <= indent:
                break
            if inner is None:
                inner = ind
            if ind != inner:
                continue
            s = ln.strip()
            m = re.match(r"(async\s+)?def\s+(\w+)\s*\((.*)", s)
            if m:
                args = m.group(3).split(")")[0]
                args = ", ".join(a.split(":")[0].split("=")[0].strip() for a in args.split(",") if a.strip() and a.strip() not in ("self", "cls"))
                name = m.group(2)
                out.append({"text": f"{name}({args})", "method": True, "visibility": "-" if name.startswith("_") and not name.startswith("__") else "+"})
            else:
                m = re.match(r"(\w+)\s*(:\s*([^=]+))?=?", s)
                if m and not s.startswith(("class ", "@", "return", "if ", "for ", "with ", "try", "pass", '"', "'")) and (":" in s or "=" in s):
                    t = f": {m.group(3).strip()}" if m.group(3) else ""
                    out.append({"text": f"{m.group(1)}{t}", "method": False, "visibility": "-" if m.group(1).startswith("_") else "+"})
            if len(out) >= MAX_MEMBERS:
                break
        return out
    # brace languages: from the first "{" to its match, collecting depth-1 declarations
    access = "+" if re.search(r"\bstruct\b|\binterface\b", lines[start]) or lang != "cpp" else "-"
    depth, opened, buf = 0, False, ""
    for ln in lines[start: start + 4000]:
        code = re.sub(r"//.*$", "", ln)
        for ch in code:
            if ch == "{":
                if opened and depth == 1 and buf.strip():
                    mem = _member(buf, lang)
                    if mem:
                        mem["visibility"] = access
                        out.append(mem)
                    buf = ""
                depth += 1
                if not opened:
                    opened, buf = True, ""
                    continue
            elif ch == "}":
                depth -= 1
                if opened and depth == 0:
                    return out[:MAX_MEMBERS]
                if depth == 1:
                    buf = ""
                continue
            if opened and depth == 1:
                buf += ch
                if ch == ";":
                    mem = _member(buf, lang)
                    if mem:
                        mem["visibility"] = access
                        out.append(mem)
                    buf = ""
                elif ch == ":" and re.fullmatch(r"\s*(public|protected|private)\s*:", buf):
                    access = {"public": "+", "protected": "#", "private": "-"}[buf.strip()[:-1].strip()]
                    buf = ""
        if opened and depth == 1:
            buf += " "
        if len(out) >= MAX_MEMBERS:
            break
    return out[:MAX_MEMBERS]


_CLASS_LINE = re.compile(r"^\s*(?:template\s*<.*>\s*)?(?:export\s+)?(?:(?:public|private|internal|abstract|final|sealed|data|open|static)\s+)*"
                         r"(?:class|struct|interface)\s+(?:[A-Za-z_][\w:()]*\s+)*(?!final\b)([A-Za-z_]\w*)\s*(?=final\b|[:{<(]|$)")


def class_spans(lines: list[str], lang: str) -> list[tuple[int, int, str]]:
    """(first line, last line, name) of every class / struct of a file (1-based), by braces or indentation."""
    out = []
    for i, ln in enumerate(lines):
        m = _CLASS_LINE.match(ln)
        if not m or (re.search(r";\s*$", ln) and "{" not in ln):
            continue
        if lang == "py":
            ind = len(ln) - len(ln.lstrip())
            j = i + 1
            last = i
            while j < len(lines):
                t = lines[j]
                if t.strip() and not t.lstrip().startswith("#"):
                    if len(t) - len(t.lstrip()) <= ind:
                        break
                    last = j
                j += 1
            out.append((i + 1, last + 1, m.group(1)))
            continue
        depth, opened, last = 0, False, None
        for j in range(i, min(len(lines), i + 20000)):
            code = re.sub(r"//.*$", "", lines[j])
            if not opened and j > i + 15:
                break
            if not opened and ";" in code and "{" not in code and j > i:
                break  # a declaration after all
            for ch in code:
                if ch == "{":
                    depth += 1
                    opened = True
                elif ch == "}":
                    depth -= 1
            if opened and depth <= 0:
                last = j
                break
        if last is not None:
            out.append((i + 1, last + 1, m.group(1)))
    return out


_TEST_PATH = re.compile(r"(^|/)(tests?|testing|unittests?|__tests__|test_utils?)/|(^|/)test_[^/]*$|_tests?\.\w+$|\.(test|spec)\.\w+$")
_TEST_NAME = re.compile(r"^(Test|Mock|Fake)[A-Z_]|^test_|(Test|Tests|TestCase|TestBase|Mock|Fake)$")


def is_test(name: str, path: str | None) -> bool:
    """A test class / file: in a test folder or file, or named like one (TestFoo, FooTest, MockFoo, FakeFoo)."""
    return bool((path and _TEST_PATH.search(path)) or _TEST_NAME.search(name or ""))


def _lang(path: str) -> str:
    return "py" if path.endswith((".py", ".pyi")) else "cpp" if re.search(r"\.(h|hh|hpp|hxx|cuh|inl|c|cc|cpp|cxx|cu)$", path) else "other"


class Uml:
    def __init__(self, files: DevFiles):
        self.f = files
        self.t = files.t

    async def _grep(self, root: str, args: list[str], paths: list[str] | None = None) -> list[tuple[str, int, str]]:
        rc, out, _ = await self.t.run(["git", "-C", root, "grep", "--no-color", "-n", "-I", "--untracked", *args, "--", *(paths or SOURCE_GLOBS)],
                                      timeout=120)
        if rc not in (0, 1):
            return []
        hits = []
        for line in out.splitlines()[:MAX_GREP]:
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                hits.append((parts[0], int(parts[1]), parts[2]))
        return hits

    async def _lines(self, root_name: str, rel: str, cache: dict[str, list[str]]) -> list[str]:
        if rel not in cache:
            try:
                _, data = await self.f.read_bytes(root_name, rel)
                cache[rel] = data.decode("utf-8", "replace").split("\n")
            except Exception:
                cache[rel] = []
        return cache[rel]

    async def _describe(self, root_name: str, path: str, line: int, name: str, cache: dict[str, list[str]], members: bool) -> dict[str, Any]:
        lines = await self._lines(root_name, path, cache)
        i = line - 1
        if _lang(path) == "py":
            hdr = []
            for ln in lines[i: i + 12]:
                hdr.append(ln)
                if re.sub(r"#.*$", "", ln).rstrip().endswith(":"):
                    break
            info = parse_header("\n".join(hdr), name)
        else:
            header = "\n".join(lines[i: i + 12]) if lines else ""
            cut = re.search(r"[{;]", header)
            info = parse_header(header[: cut.end()] if cut else header, name)
        out = {"name": name, "path": path, "line": line, **info}
        if members and lines:
            out["members"] = scan_members(lines, i, _lang(path))
        return out

    async def find(self, root: str, root_name: str, name: str, near: str | None, cache: dict[str, list[str]], members: bool) -> dict[str, Any] | None:
        hits = [(p, n, t) for p, n, t in await self._grep(root, ["-E", "-e", _def_regex(name)])
                if not re.search(rf"\b{re.escape(name)}\s*;\s*$", t)]  # not `class X;`
        if not hits:
            return None
        here = (near or "").rsplit("/", 1)[0]

        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()

        def key(h: tuple[str, int, str]) -> tuple:
            p = h[0]
            test = bool(re.search(r"(^|/)(tests?|testing|benchmarks?|_?export|third_party)/|(^|/)test_", p))
            stem = p.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            return (p != near, test, stem not in (name.lower(), snake), not p.startswith(here) if here else False, not re.search(r"\.(h|hh|hpp|py|java|ts|cs)$", p), len(p), p, h[1])

        best = sorted(hits, key=key)
        for p, n, _ in best[:5]:  # the first whose header really names it (not a same-named wrapper)
            d = await self._describe(root_name, p, n, name, cache, members)
            if d and name not in d["bases"]:
                d["others"] = len(hits) - 1
                return d
        return None

    async def diagram(self, root_name: str, name: str, near: str | None = None, parts: set[str] | None = None,
                      at: tuple[str, int] | None = None, tests: bool = False) -> dict[str, Any]:
        """The diagram around `name`, or only some of its parts (`class`, `parents`, `children`, `users`) so the page
        can show the class first and add the rest as it comes. `at` (path, line): the class's definition, already
        known (no search for it)."""
        if not IDENT.match(name or ""):
            return {"error": "not a class name"}
        parts = parts or set(PARTS)
        root = await self.f.root(root_name)
        cache: dict[str, list[str]] = {}
        w = re.escape(name)
        pats = [rf"(class|struct)\s+([A-Za-z_][A-Za-z_0-9:()]*\s+)*[A-Za-z_]\w*\s*(final\s*)?(<[^>]*>\s*)?:[^;{{]*\b{w}\b",
                rf"^\s*class\s+\w+\s*\([^)]*\b{w}\b",
                rf"(class|interface)\s+\w+.*\b(extends|implements)\b[^{{]*\b{w}\b"]
        args = ["-E"]
        for p in pats:
            args += ["-e", p]
        kids_task = asyncio.ensure_future(self._grep(root, args)) if "children" in parts else None
        uses_task = asyncio.ensure_future(self._grep(root, ["-F", "-w", "-e", name])) if "users" in parts else None
        if at:
            cls = await self._describe(root_name, at[0], at[1], name, cache, members="class" in parts)
        else:
            cls = await self.find(root, root_name, name, near, cache, members="class" in parts)
        out: dict[str, Any] = {"name": name, "class": cls}
        if cls is None:
            for t in (kids_task, uses_task):
                if t:
                    t.cancel()
            return {**out, "parents": [], "children": [], "users": []}
        parents: list[dict[str, Any]] = []
        if "parents" not in parts:
            level: list[tuple[str, str]] = []
        else:
            level = [(b, cls["name"]) for b in cls["bases"]]
        # parents, level by level
        seen = {name}
        depth = 1
        while level and depth <= MAX_DEPTH:
            nxt = []
            todo = [(b, child) for b, child in level if b not in seen]
            seen |= {b for b, _ in todo}
            todo = list({b: (b, c) for b, c in todo}.values())
            found = await asyncio.gather(*(self.find(root, root_name, b, cls["path"], cache, members=depth == 1) for b, _ in todo))
            for (b, child), d in zip(todo, found, strict=True):
                entry = d or {"name": b, "path": None, "line": None, "bases": [], "kind": "class"}
                entry.update(depth=depth, child=child)
                parents.append(entry)
                nxt += [(bb, b) for bb in entry.get("bases", [])]
            level, depth = nxt, depth + 1
        if "parents" in parts:
            out["parents"] = parents
        # children: classes whose base list names it
        children: list[dict[str, Any]] = []
        kids_seen: set[str] = set()
        hidden_tests: list[str] = []
        for p, n, t in (await kids_task if kids_task else []):
            m = re.search(r"\b(class|struct|interface)\s+(?:[A-Za-z_][\w:()]*\s+)*(?!final\b)([A-Za-z_]\w*)\s*(?:final\s*)?(?:<[^>]*>\s*)?(?:\(|:(?!:)|\bextends\b|\bimplements\b)", t)
            if not m:
                continue
            kid = m.group(2)
            if kid == name or kid in kids_seen or name not in parse_header(t if t.rstrip().endswith(("{", ":")) else t + " {", kid)["bases"]:
                continue
            if not tests and is_test(kid, p):  # test classes are left out unless asked for
                hidden_tests.append(kid)
                kids_seen.add(kid)
                continue
            kids_seen.add(kid)
            children.append({"name": kid, "path": p, "line": n, "kind": m.group(1)})
            if len(children) >= MAX_CHILDREN:
                break
        if "children" in parts:
            out["children"] = children
            out["hidden_tests"] = hidden_tests
        if not uses_task:
            return out
        # users: where the name is used, attributed to the class around the use (or the file)
        uses = [(p, n) for p, n, t in await uses_task]
        files = sorted({p for p, _ in uses})
        known = {name} | {x["name"] for x in parents} | {x["name"] for x in children}
        spans: dict[str, list[tuple[int, int, str]]] = {}
        for p in files[:200]:  # the classes of each using file, with where they end
            spans[p] = class_spans(await self._lines(root_name, p, cache), _lang(p))
        users: dict[tuple[str, str], dict[str, Any]] = {}
        for p, n in uses:
            if p == cls["path"] and abs(n - cls["line"]) < 2:
                continue
            inside = [sp for sp in spans.get(p, []) if sp[0] <= n <= sp[1]]
            owner = max(inside, key=lambda sp: sp[0]) if inside else None  # the innermost class around the use
            if owner and owner[2] == name and p == cls["path"]:
                continue  # inside the class itself
            label = owner[2] if owner else p.rsplit("/", 1)[-1]
            k = (label, p)
            u = users.setdefault(k, {"name": label, "path": p, "line": owner[0] if owner else n, "count": 0, "file": owner is None})
            u["count"] += 1
        ulist = [u for u in users.values() if u["name"] not in known or u["file"]]
        hidden_users: list[str] = []
        if not tests:
            kept = [u for u in ulist if not is_test(u["name"] if not u["file"] else "", u["path"])]
            hidden_users = sorted({u["name"] for u in ulist if u not in kept})
            ulist = kept
        ulist.sort(key=lambda u: (u["file"], -u["count"], u["path"]))
        return {**out, "users": ulist[:MAX_USERS], "more_users": max(0, len(ulist) - MAX_USERS), "use_count": len(uses),
                "hidden_tests": sorted(set(out.get("hidden_tests", [])) | set(hidden_users))}
