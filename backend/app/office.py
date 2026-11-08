"""Faithful rendering of Office documents (Word, PowerPoint, Visio, OpenDocument) to PDF with LibreOffice.

The reader's *Layout* view shows the PDF in the browser's viewer, so pages, slides and diagrams look as their
authors made them; the *Text* view (textmodel.py) stays the basis for anchored discussions and diffs.

- Results are cached in ``<state_dir>/renders/<sha256 of the file>-v<N>.pdf`` (content-addressed: an unchanged file
  is never converted twice, a changed one is converted again). The cache is capped (``[office] cache_mb``), oldest
  first, and never part of backups.
- At most ``max_parallel`` LibreOffice processes run at once, each with its own private profile under
  ``<state_dir>/lo-profiles`` (the user's own LibreOffice settings and running instances are never touched).
- The input is copied to a temporary folder first: LibreOffice never opens files in the wiki itself, so it cannot
  leave lock files there or modify them. Conversion runs headless, without macros, killed after ``timeout``.
- Failures are remembered per content hash (so a broken file does not re-run LibreOffice on every poll) until
  the user asks to retry.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger("waw.office")

RENDER_VERSION = 1  # bump to invalidate every cached render (e.g. after changing conversion options)
INSTALL_HINT = "install LibreOffice on the server (e.g. `sudo apt install libreoffice-writer libreoffice-impress libreoffice-draw`) and restart"
# variables of the server's own environment that break LibreOffice's bundled libraries / Python
_ENV_DROP = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX")


class _Job:
    def __init__(self) -> None:
        self.started = time.time()


class OfficeRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings
        configured = settings.office_soffice
        self.exe = (configured if configured and Path(configured).is_file() else None) or shutil.which("soffice") or shutil.which("libreoffice")
        self.cache = settings.state_dir / "renders"
        self.profiles = settings.state_dir / "lo-profiles"
        self.max_parallel = max(1, int(settings.office_max_parallel))
        self.timeout = max(10, int(settings.office_timeout))
        self.max_bytes = int(settings.office_max_bytes)
        self.cache_bytes = int(settings.office_cache_mb) * 1024 * 1024
        self._slots: list[int] = list(range(self.max_parallel))
        self._slot_cv = threading.Condition()
        self._lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._hashes: dict[tuple[str, int, int], str] = {}  # (path, mtime_ns, size) -> sha256

    @property
    def available(self) -> bool:
        return self.exe is not None

    def info(self) -> dict[str, Any]:
        return {"available": self.available, "install_hint": None if self.available else INSTALL_HINT,
                "max_bytes": self.max_bytes}

    # ------------------------------------------------------------------ keys and cache
    def _sha(self, path: Path) -> str:
        st = path.stat()
        k = (str(path), st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._hashes.get(k)
        if hit:
            return hit
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        sha = h.hexdigest()
        with self._lock:
            if len(self._hashes) > 5000:
                self._hashes.clear()
            self._hashes[k] = sha
        return sha

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.cache / f"{key}.pdf", self.cache / f"{key}.err"

    def key_for(self, path: Path) -> str:
        return f"{self._sha(path)}-v{RENDER_VERSION}"

    # ------------------------------------------------------------------ public API
    def status(self, path: Path, *, retry: bool = False) -> dict[str, Any]:
        """State of the PDF for this file, starting a conversion when needed. Never blocks on LibreOffice."""
        if not self.available:
            return {"state": "unavailable", "message": f"LibreOffice is not installed: {INSTALL_HINT}"}
        size = path.stat().st_size
        if size > self.max_bytes:
            return {"state": "failed", "code": "TOO_LARGE",
                    "message": f"the file is {size // (1024 * 1024)} MiB; the layout view renders files up to {self.max_bytes // (1024 * 1024)} MiB ([office] max_bytes)"}
        key = self.key_for(path)
        pdf, err = self._paths(key)
        if pdf.is_file():
            try:
                os.utime(pdf)  # recently used: kept longest when the cache is pruned
            except OSError:
                pass
            return {"state": "ready", "key": key, "bytes": pdf.stat().st_size}
        if err.is_file():
            if retry:
                err.unlink(missing_ok=True)
            else:
                return {"state": "failed", "code": "RENDER_FAILED", "key": key, "message": err.read_text(errors="replace")[:500]}
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                job = self._jobs[key] = _Job()
                threading.Thread(target=self._run, args=(key, path), daemon=True, name=f"lo-{key[:8]}").start()
        waiting = self._waiting_for(key)
        return {"state": "pending", "key": key, "seconds": round(time.time() - job.started), "queued": waiting}

    def pdf_path(self, path: Path) -> Path | None:
        pdf, _ = self._paths(self.key_for(path))
        return pdf if pdf.is_file() else None

    # ------------------------------------------------------------------ conversion (worker threads)
    def _waiting_for(self, key: str) -> bool:
        with self._lock:
            running = len(self._jobs)
        return running > self.max_parallel

    def _take_slot(self) -> int:
        with self._slot_cv:
            while not self._slots:
                self._slot_cv.wait()
            return self._slots.pop()

    def _give_slot(self, slot: int) -> None:
        with self._slot_cv:
            self._slots.append(slot)
            self._slot_cv.notify()

    def _run(self, key: str, src: Path) -> None:
        slot = self._take_slot()
        pdf, err = self._paths(key)
        started = time.time()
        try:
            self.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp_root = self.cache / "tmp"
            tmp_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
                work = Path(tmp)
                (work / "in").mkdir()
                (work / "out").mkdir()
                # a neutral name with the original extension (it decides the import filter)
                local = work / "in" / f"document{src.suffix.lower()}"
                shutil.copyfile(src, local)
                message = self._convert(local, work / "out", slot)
                out = work / "out" / "document.pdf"
                if message is None and out.is_file() and out.stat().st_size > 0:
                    os.replace(out, pdf)
                    log.info("rendered %s in %.1f s (%d KiB)", src.name, time.time() - started, pdf.stat().st_size // 1024)
                else:
                    err.write_text(message or "LibreOffice produced no PDF (the file may be damaged or password-protected)")
                    log.warning("could not render %s: %s", src.name, err.read_text()[:200])
        except Exception as exc:  # never let a worker die silently
            log.exception("rendering %s failed", src)
            try:
                err.write_text(f"{exc.__class__.__name__}: {exc}")
            except OSError:
                pass
        finally:
            with self._lock:
                self._jobs.pop(key, None)
            self._give_slot(slot)
        self._prune()

    def _convert(self, local: Path, outdir: Path, slot: int) -> str | None:
        """Run one headless conversion; None on success, else a message."""
        assert self.exe
        profile = self.profiles / f"slot-{slot}"
        profile.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP}
        env["HOME"] = str(profile)  # LibreOffice writes caches under $HOME too
        cmd = [self.exe, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless", "--invisible", "--norestore",
               "--nologo", "--nodefault", "--nolockcheck", "--convert-to", "pdf", "--outdir", str(outdir), str(local)]
        proc = subprocess.Popen(cmd, cwd=str(outdir), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            output, _ = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            return f"LibreOffice did not finish within {self.timeout} s ([office] timeout)"
        if proc.returncode != 0:
            text = output.decode(errors="replace").strip().splitlines()
            useful = [ln for ln in text if "libcurl" not in ln]
            return f"LibreOffice failed (exit {proc.returncode}): {' '.join(useful[-3:])[:300]}"
        return None

    def _prune(self) -> None:
        """Keep the cache under its size cap, removing the least recently used renders first."""
        try:
            files = [p for p in self.cache.glob("*.pdf") if p.is_file()]
            total = sum(p.stat().st_size for p in files)
            if total <= self.cache_bytes:
                return
            for p in sorted(files, key=lambda p: p.stat().st_mtime):
                if total <= self.cache_bytes * 0.8:
                    break
                total -= p.stat().st_size
                p.unlink(missing_ok=True)
            for e in self.cache.glob("*.err"):  # failure notes are tiny, but bound their number too
                if e.stat().st_mtime < time.time() - 30 * 86400:
                    e.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("pruning the render cache failed: %s", exc)
