"""Go to definition in a PR's diff: where a name (function, class, macro, type, variable …) is defined.

Text search with per-language definition patterns, no language server: in the connected local checkout (the PR's
head fetched as `refs/waw/pr/<n>`, see local_repos) the whole repository at the PR's head is searched with
`git grep`; without a checkout only the PR's changed files (at its head, from GitHub) are. Results are ranked: the
file the name was clicked in first, then nearby files, then the rest; tests last.
"""

from __future__ import annotations

import posixpath
import re
from typing import Any, Iterable

from .local_repos import git

MAX_RESULTS = 40
MAX_GREP_LINES = 50_000
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,199}")
_KEYWORDS = {"return", "if", "else", "while", "for", "switch", "case", "new", "delete", "throw", "co_return", "sizeof",
             "do", "goto", "not", "and", "or", "in", "is", "await", "yield", "assert", "print"}


def _patterns(s: str) -> list[tuple[str, re.Pattern[str]]]:
    e = re.escape(s)
    p = [
        # Python
        ("def", rf"^\s*(async\s+)?def\s+{e}\s*[\[(]"),
        ("class", rf"^\s*class\s+{e}\b"),
        ("variable", rf"^{e}\s*(:[^=]+)?=(?!=)"),
        # C / C++ / CUDA / SYCL
        ("macro", rf"^\s*#\s*define\s+{e}\b"),
        ("type", rf"^\s*(template\s*<.*>\s*)?(typedef\s+)?(class|struct|union|enum(\s+class|\s+struct)?|concept)\s+([\w:]+\s+)*{e}\b(?!\s*;)"),
        ("type", rf"^\s*using\s+{e}\s*="),
        ("type", rf"^\s*typedef\b.*\b{e}\s*;"),
        ("namespace", rf"^\s*(inline\s+)?namespace\s+([\w:]+::)?{e}\b"),
        ("function", rf"^\s*[\w:<>,*&\s~\[\]]*[\w>*&\]]\s+[*&]*\s*(\w+::)*~?{e}\s*\((?!.*;\s*$)"),
        ("function", rf"^(\w+::)+~?{e}\s*\((?!.*;\s*$)"),
        # JavaScript / TypeScript
        ("function", rf"^\s*(export\s+)?(default\s+)?(async\s+)?function\s*\*?\s*{e}\s*[<(]"),
        ("variable", rf"^\s*(export\s+)?(const|let|var)\s+{e}\s*[:=]"),
        ("type", rf"^\s*(export\s+)?(declare\s+)?(abstract\s+)?(class|interface|type|enum)\s+{e}\b"),
        # Go / Rust
        ("function", rf"^\s*func\s+(\([^)]*\)\s*)?{e}\s*[\[(]"),
        ("function", rf"^\s*(pub(\([^)]*\))?\s+)?(const\s+|async\s+|unsafe\s+)*fn\s+{e}\b"),
        ("type", rf"^\s*(pub(\([^)]*\))?\s+)?(struct|enum|trait|type|mod)\s+{e}\b(?!\s*;)"),
        # PyTorch's native_functions.yaml / derivatives.yaml
        ("operator", rf"^\s*-\s*(func|name):\s*{e}[.(]"),
    ]
    return [(k, re.compile(x)) for k, x in p]


def definitions_in(path: str, lines: Iterable[tuple[int, str]], symbol: str) -> list[dict[str, Any]]:
    pats = _patterns(symbol)
    out = []
    for no, text in lines:
        if symbol not in text:
            continue
        for kind, rx in pats:
            if rx.search(text):
                if kind == "function" and (text.strip().split(None, 1) or [""])[0] in _KEYWORDS:
                    break
                out.append({"path": path, "line": no, "text": text.strip()[:300], "kind": kind})
                break
    return out


def rank(results: list[dict[str, Any]], origin: str | None) -> list[dict[str, Any]]:
    here = posixpath.dirname(origin or "")

    def key(r: dict[str, Any]) -> tuple:
        p = r["path"]
        common = posixpath.commonpath([here, posixpath.dirname(p)]) if here else ""
        test = bool(re.search(r"(^|/)(tests?|testing|benchmarks?)/|(^|/)test_[^/]*$|_test\.\w+$", p))
        return (p != origin, test, -len(common.split("/")) if common else 0, p, r["line"])

    seen, out = set(), []
    for r in sorted(results, key=key):
        k = (r["path"], r["line"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out[:MAX_RESULTS]


def grep_checkout(path: str, ref: str, symbol: str) -> list[dict[str, Any]] | None:
    """Every definition of `symbol` in the commit `ref` of the checkout at `path`; None when git failed."""
    code, out, _ = git(path, ["grep", "-n", "-I", "-w", "-F", "--no-color", "-e", symbol, ref, "--"], 60)
    if code not in (0, 1):  # 1: no match
        return None
    by_file: dict[str, list[tuple[int, str]]] = {}
    prefix = f"{ref}:"
    for line in out.splitlines()[:MAX_GREP_LINES]:
        if line.startswith(prefix):
            line = line[len(prefix):]
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            by_file.setdefault(parts[0], []).append((int(parts[1]), parts[2]))
    found: list[dict[str, Any]] = []
    for f, ls in by_file.items():
        found += definitions_in(f, ls, symbol)
    return found


def commit_present(path: str, ref: str) -> bool:
    return git(path, ["cat-file", "-e", f"{ref}^{{commit}}"], 20)[0] == 0
