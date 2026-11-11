"""Code navigation for every file type, without a language server: go to definition and find references by searching
the worktree (git grep) with per-language definition patterns, and links inside strings — Bazel labels
(`//pkg:target`, `:target`, `@repo//pkg:file.bzl`) and relative file paths — resolved to files and lines.

The web editor uses it for files whose language has no running language server (YAML, Starlark/Bazel, CMake, TOML,
JSON, shell, Makefile, … and C++/Python when clangd/pyright are missing); a language server's answers win.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from .files import DevFiles, norm_rel

MAX_HITS = 200
WORD = re.compile(r"^[A-Za-z_$][\w$.-]{0,200}$")

# extended regexes (git grep -E); {w} = the escaped word
DEF_PATTERNS: dict[str, list[str]] = {
    "python": [r"^\s*(async\s+)?def\s+{w}\b", r"^\s*class\s+{w}\b", r"^{w}\s*(:[^=]*)?=[^=]"],
    "starlark": [r"^\s*def\s+{w}\b", r"^{w}\s*=[^=]", r"name\s*=\s*\"{w}\"", r"^\s*{w}\s*=\s*(rule|macro|provider|repository_rule|aspect|module_extension|tag_class)\("],
    "cpp": [r"^\s*(class|struct|union|enum( class)?|namespace)\s+{w}\b", r"^#\s*define\s+{w}\b",
            r"^[^;(){{}}=]*[\w>*&:]\s+\**{w}\s*\([^;]*$", r"^\s*(using|typedef)\b.*\b{w}\s*[=;]"],
    "c": [r"^\s*(struct|union|enum)\s+{w}\b", r"^#\s*define\s+{w}\b", r"^[^;(){{}}=]*[\w*]\s+\**{w}\s*\([^;]*$", r"^\s*typedef\b.*\b{w}\s*;"],
    "yaml": [r"^\s*-?\s*{w}\s*:", r"&{w}\b"],
    "json": [r"\"{w}\"\s*:"],
    "toml": [r"^\s*\[+\s*{w}\s*\]+", r"^\s*{w}\s*="],
    "ini": [r"^\s*\[\s*{w}\s*\]", r"^\s*{w}\s*="],
    "cmake": [r"^\s*(function|macro)\s*\(\s*{w}\b", r"^\s*add_(library|executable|custom_target)\s*\(\s*{w}\b", r"^\s*set\s*\(\s*{w}\b",
              r"^\s*option\s*\(\s*{w}\b"],
    "shell": [r"^\s*(function\s+)?{w}\s*\(\s*\)", r"^\s*function\s+{w}\b", r"^\s*(export\s+|local\s+|readonly\s+)?{w}="],
    "makefile": [r"^{w}\s*:", r"^{w}\s*[:+?]?="],
    "dockerfile": [r"^\s*FROM\s+\S+\s+AS\s+{w}\b", r"^\s*(ARG|ENV)\s+{w}\b"],
    "markdown": [r"^#+\s+.*{w}"],
    "protobuf": [r"^\s*(message|enum|service|rpc|extend)\s+{w}\b"],
}
GENERIC = [r"\b(def|class|function|fn|func|struct|enum|interface|type|trait|impl|module|macro|const|let|var|val|sub|proc)\s+{w}\b",
           r"^\s*{w}\s*[:=]", r"^#\s*define\s+{w}\b"]
LANG_GLOBS: dict[str, list[str]] = {
    "starlark": ["*.bzl", "*.bazel", "*.star", "BUILD", "WORKSPACE", "*.BUILD"],
    "python": ["*.py", "*.pyi"],
    "cpp": ["*.h", "*.hh", "*.hpp", "*.hxx", "*.c", "*.cc", "*.cpp", "*.cxx", "*.cu", "*.cuh", "*.inl", "*.ipp", "*.tpp"],
    "c": ["*.h", "*.c"],
    "yaml": ["*.yaml", "*.yml"],
    "json": ["*.json", "*.jsonc"],
    "toml": ["*.toml"],
    "cmake": ["CMakeLists.txt", "*.cmake"],
    "shell": ["*.sh", "*.bash", "*.zsh"],
    "makefile": ["Makefile", "*.mk", "GNUmakefile"],
    "protobuf": ["*.proto"],
}
LABEL = re.compile(r"^(@[\w.~+-]*)?(//)?([\w./+=,@~ -]*)(:([\w./+=,@~ -]+))?$")
BUILD_FILES = ("BUILD.bazel", "BUILD")


def _string_at(line: str, col: int) -> str | None:
    """The quoted string the (1-based) column is in, if any."""
    i = col - 1
    for q in ('"', "'"):
        start = line.rfind(q, 0, i + 1 if i < len(line) and line[i] != q else i)
        if start < 0:
            continue
        end = line.find(q, max(i, start + 1))
        if end > start and start < i <= end:
            return line[start + 1:end]
    return None


class Nav:
    def __init__(self, files: DevFiles):
        self.f = files
        self.t = files.t

    async def _exists(self, path: str) -> bool:
        rc, _, _ = await self.t.run(["test", "-f", path], timeout=20)
        return rc == 0

    async def _grep(self, root: str, args: list[str], globs: list[str] | None, timeout: float = 60) -> list[dict[str, Any]]:
        argv = ["git", "-C", root, "grep", "--no-color", "-n", "-I", "--untracked", *args]
        if globs:
            argv += ["--", *globs]
        rc, out, _ = await self.t.run(argv, timeout=timeout)
        if rc not in (0, 1):
            return []
        hits = []
        for line in out.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                hits.append({"path": parts[0], "line": int(parts[1]), "text": parts[2][:300]})
            if len(hits) >= MAX_HITS:
                break
        return hits

    # ------------------------------------------------------------------ labels / paths
    async def resolve_label_or_path(self, root_name: str, rel: str, text: str) -> list[dict[str, Any]]:
        root = await self.f.root(root_name)
        here = rel.rsplit("/", 1)[0] if "/" in rel else ""
        out: list[dict[str, Any]] = []
        s = text.strip()
        if not s or len(s) > 500 or "\0" in s:
            return out
        m = LABEL.match(s)
        if m and (m.group(2) or s.startswith(":")) and not m.group(1):  # //pkg:target, //pkg, :target (same repo)
            pkg = (m.group(3) or "").strip("/") if m.group(2) else here
            target = m.group(5) or (pkg.rsplit("/", 1)[-1] if pkg else "")
            try:
                pkg = norm_rel(pkg)
            except Exception:
                return out
            # a source file of the package (e.g. defs.bzl, foo.cc)
            if target and "." in target.rsplit("/", 1)[-1]:
                cand = f"{pkg}/{target}" if pkg else target
                if await self._exists(self.f.join(root, cand)):
                    return [{"path": cand, "line": 1, "col": 1}]
            for bf in BUILD_FILES:
                cand = f"{pkg}/{bf}" if pkg else bf
                if await self._exists(self.f.join(root, cand)):
                    line = 1
                    if target:
                        rc, txt, _ = await self.t.run(["grep", "-n", "-E", rf"name\s*=\s*[\"']{re.escape(target)}[\"']", self.f.join(root, cand)], timeout=20)
                        if rc == 0 and txt.strip():
                            line = int(txt.split(":", 1)[0])
                    return [{"path": cand, "line": line, "col": 1}]
            return out
        # a relative path (from this file's folder, then from the worktree root)
        if "/" in s or "." in s:
            for base in ([here] if here else []) + [""]:
                try:
                    cand = norm_rel(f"{base}/{s}" if base else s)
                except Exception:
                    continue
                if cand and await self._exists(self.f.join(root, cand)):
                    return [{"path": cand, "line": 1, "col": 1}]
        return out

    # ------------------------------------------------------------------ symbols
    async def definition(self, root_name: str, rel: str, word: str, language: str, line_text: str, col: int) -> list[dict[str, Any]]:
        s = _string_at(line_text, col) if line_text else None
        if s:
            hit = await self.resolve_label_or_path(root_name, rel, s)
            if hit:
                return hit
        if not WORD.match(word or ""):
            return []
        root = await self.f.root(root_name)
        w = re.escape(word).replace("/", r"\/")
        pats = DEF_PATTERNS.get(language, []) or []
        pats = [p.replace("{w}", w) for p in pats] or [p.replace("{w}", w) for p in GENERIC]
        args: list[str] = ["-E"]
        for p in pats:
            args += ["-e", p]
        globs = LANG_GLOBS.get(language)
        hits = await self._grep(root, args, globs)
        if not hits and globs:
            hits = await self._grep(root, args, None)  # e.g. a .bzl symbol defined in a BUILD file elsewhere
        # this file first, then by path
        hits.sort(key=lambda h: (h["path"] != rel, h["path"], h["line"]))
        for h in hits:
            c = h["text"].find(word)
            h["col"] = c + 1 if c >= 0 else 1
        return hits[:50]

    async def references(self, root_name: str, word: str, language: str) -> list[dict[str, Any]]:
        if not WORD.match(word or ""):
            return []
        root = await self.f.root(root_name)
        hits = await self._grep(root, ["-F", "-w", "-e", word], None)
        for h in hits:
            c = h["text"].find(word)
            h["col"] = c + 1 if c >= 0 else 1
        return hits


def quote(s: str) -> str:
    return shlex.quote(s)
