"""docs/SCHEMA.md must describe every table and column of the migrated database."""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa

from conftest import ROOT

DOC = ROOT / "docs" / "SCHEMA.md"


def test_every_table_and_column_is_documented(settings):
    from backend.app.db import Database

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    db.migrate()
    insp = sa.inspect(db.engine)
    text = DOC.read_text("utf-8")
    sections = {m.group(1): m.start() for m in re.finditer(r"^## `([a-z_]+)`", text, re.M)}
    missing: list[str] = []
    for table in insp.get_table_names():
        if table == "alembic_version":
            assert "alembic_version" in text
            continue
        if table not in sections:
            missing.append(f"table {table}")
            continue
        start = sections[table]
        end = min([p for p in sections.values() if p > start] + [len(text)])
        section = text[start:end]
        for col in insp.get_columns(table):
            if f"`{col['name']}`" not in section:
                missing.append(f"{table}.{col['name']}")
    assert not missing, "undocumented in docs/SCHEMA.md: " + ", ".join(missing)
