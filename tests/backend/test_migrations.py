"""Migrations must work on a populated database, not only on an empty one (SQLite batch mode drops/recreates
tables that other tables reference)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.app.db import Database


def _populate_v1(path: Path) -> None:
    """Create a database at the initial revision with rows in referencing tables, as a real deployment has."""
    from alembic import command
    from alembic.config import Config

    import sqlalchemy as sa

    root = Path(__file__).resolve().parents[2]
    cfg = Config()
    cfg.set_main_option("script_location", str(root / "backend" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    eng = sa.create_engine(f"sqlite+pysqlite:///{path}")
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0001_initial")
    eng.dispose()
    con = sqlite3.connect(path)
    con.executescript("""
        INSERT INTO workspaces(id, root_path, created_at) VALUES ('ws', '/w', '2026-01-01 00:00:00');
        INSERT INTO agent_sessions(id, provider, native_id, workspace_id, display_name, status, origin, created_at, updated_at)
            VALUES ('s1', 'opencode', 'ses_1', 'ws', 'n', 'ready', 'app', '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        INSERT INTO documents(id, workspace_id, path, default_session_id, last_sha256, missing, created_at, updated_at)
            VALUES ('d1', 'ws', 'a.md', 's1', 'x', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        INSERT INTO document_versions(id, document_id, sha256, snapshot_ref, source, created_at) VALUES ('v1', 'd1', 'x', 'd1/x.md', 'observed', '2026-01-01 00:00:00');
        INSERT INTO threads(id, document_id, orig_version_sha256, orig_quote, orig_prefix, orig_suffix, orig_start, orig_end,
            cur_version_sha256, cur_quote, cur_prefix, cur_suffix, cur_start, cur_end, anchor_state, last_session_id, created_at, updated_at)
            VALUES ('t1', 'd1', 'x', 'q', '', '', 0, 1, 'x', 'q', '', '', 0, 1, 'attached', 's1', '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        INSERT INTO messages(id, thread_id, session_id, role, content, status, created_at) VALUES ('m1', 't1', 's1', 'user', 'hi', 'completed', '2026-01-01 00:00:00');
        INSERT INTO reading_state(document_id, offset, scroll_fraction, updated_at) VALUES ('d1', 0, 0.5, '2026-01-01 00:00:00');
        INSERT INTO review_marks(id, document_id, quote, prefix, suffix, start, "end", reviewed_version_sha256, state, created_at, updated_at)
            VALUES ('r1', 'd1', 'q', '', '', 0, 1, 'x', 'reviewed', '2026-01-01 00:00:00', '2026-01-01 00:00:00');
    """)
    con.commit()
    con.close()


def test_upgrade_from_initial_schema_with_data(tmp_path: Path):
    db_path = tmp_path / "w.sqlite3"
    _populate_v1(db_path)
    db = Database(db_path)
    db.migrate()  # must not raise (previously: FOREIGN KEY constraint failed on DROP TABLE documents)
    con = sqlite3.connect(db_path)
    cols = {r[1] for r in con.execute("pragma table_info(documents)")}
    assert {"hidden"} <= cols
    assert "diff_json" in {r[1] for r in con.execute("pragma table_info(threads)")}
    assert "locks" in {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    # data and references survived the table rebuilds
    assert con.execute("select count(*) from threads where document_id='d1'").fetchone()[0] == 1
    assert con.execute("select hidden from documents where id='d1'").fetchone()[0] == 0
    assert con.execute("pragma foreign_key_check").fetchall() == []
    assert con.execute("pragma foreign_keys").fetchone()[0] in (0, 1)
    con.close()
    # the app's own connections keep enforcing foreign keys after migration
    with db.read() as s:
        assert s.execute(__import__("sqlalchemy").text("pragma foreign_keys")).scalar() == 1
