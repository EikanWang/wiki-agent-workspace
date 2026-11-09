"""Documentation stays consistent with the code: links resolve, every config key is described."""

from __future__ import annotations

import re
import tomllib

from conftest import ROOT

DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def test_relative_links_in_docs_resolve():
    broken = []
    for doc in DOCS:
        text = doc.read_text("utf-8")
        for m in re.finditer(r"\]\(([^)\s]+)\)", text):
            target = m.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            if not (doc.parent / path).exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert not broken, "broken links: " + ", ".join(broken)


def test_every_config_key_is_documented():
    example = tomllib.loads((ROOT / "config.example.toml").read_text("utf-8"))
    conf_doc = (ROOT / "docs" / "CONFIGURATION.md").read_text("utf-8")

    def keys(d, prefix=""):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from keys(v, f"{prefix}{k}.")
            else:
                yield prefix + k, k

    def tables(d):
        for k, v in d.items():
            if isinstance(v, dict):
                yield k
                yield from tables(v)

    missing = [full for full, leaf in keys(example) if f"`{leaf}`" not in conf_doc]
    assert not missing, "config keys missing from docs/CONFIGURATION.md: " + ", ".join(missing)
    # keys used by the loader must appear in the example file too
    loader = (ROOT / "backend" / "app" / "config.py").read_text("utf-8")
    used = set(re.findall(r'(?:server|ws|term|auth|agents|office|gh)\.get\("([a-z_]+)"', loader))
    example_leaves = {leaf for _, leaf in keys(example)} | set(tables(example))
    unknown = sorted(k for k in used if k not in example_leaves)
    assert not unknown, "config keys read by config.py but absent from config.example.toml: " + ", ".join(unknown)


def test_env_overrides_documented():
    loader = (ROOT / "backend" / "app" / "config.py").read_text("utf-8")
    env_vars = set(re.findall(r'env\.get\("(WAW_[A-Z_]+)"', loader)) | {"WAW_CONFIG", "WAW_ACCESS_TOKEN"}
    conf_doc = (ROOT / "docs" / "CONFIGURATION.md").read_text("utf-8")
    missing = sorted(v for v in env_vars if f"`{v}`" not in conf_doc)
    assert not missing, "env vars missing from docs/CONFIGURATION.md: " + ", ".join(missing)
