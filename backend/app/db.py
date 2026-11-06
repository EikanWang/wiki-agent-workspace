"""SQLite (WAL) + SQLAlchemy session management with serialized writes."""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite+pysqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_pre_ping=False,
        )

        @event.listens_for(self.engine, "connect")
        def _on_connect(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        # SQLite allows one writer at a time; serialize application writes so that
        # concurrent request handlers never hit SQLITE_BUSY aborts mid-transaction.
        self.write_lock = threading.RLock()

    def migrate(self) -> None:
        from alembic import command
        from alembic.config import Config

        root = Path(__file__).resolve().parent.parent
        cfg = Config()
        cfg.set_main_option("script_location", str(root / "migrations"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{self.path}")
        # SQLite cannot ALTER columns; Alembic's batch mode recreates tables (copy, drop, rename). Dropping a table
        # that other tables reference fails while foreign_keys=ON, so migrations run with the pragma off (as the
        # Alembic SQLite documentation requires) and the referential check is re-run afterwards.
        with self.engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")  # must be set outside a transaction
            conn.commit()
            # An interrupted batch migration can leave Alembic's temporary copy behind; it is always safe to drop
            # (the rename into place is the last step of a successful batch).
            leftovers = [r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '\\_alembic\\_tmp\\_%' ESCAPE '\\'").fetchall()]
            for name in leftovers:
                conn.exec_driver_sql(f'DROP TABLE "{name}"')
            conn.commit()  # the SELECT above autobegan a transaction; end it before alembic's own begin()
            with conn.begin():
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")
            problems = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            conn.commit()
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            conn.commit()
        if problems:
            raise RuntimeError(f"database migration left {len(problems)} dangling foreign-key references: {problems[:5]}")

    def checkpoint(self) -> None:
        """Fold the WAL into the main database file so a plain copy of workspace.sqlite3 is complete."""
        try:
            with self.write_lock, self.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # pragma: no cover - best effort at shutdown
            pass

    def create_all_for_tests(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def read(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
        finally:
            s.close()

    @contextmanager
    def write(self) -> Iterator[Session]:
        with self.write_lock:
            s = self._factory()
            try:
                yield s
                s.commit()
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()
