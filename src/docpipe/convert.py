"""Local pass 2: convert every unique document to markdown with MarkItDown.

Conversion runs in child processes so a single pathological file (a
hanging parser, a native-library crash) cannot stall the pipeline: each
file gets a time budget, and on timeout or crash that worker is killed and
restarted. PDFs that cannot be converted locally are not failures - they
are flagged needs_ocr and Gemini reads the original file directly.

Each worker is a plain subprocess speaking JSON-lines over stdin/stdout
(not multiprocessing, whose spawn mode re-imports the parent's __main__
and so breaks depending on how the tool was launched). Library noise goes
to the child's /dev/null'd stderr and a redirected stdout.

Conversion is CPU-bound and embarrassingly parallel, so a pool of workers
runs several files at once. The child protocol is a strict request /
response over one pipe with no correlation ids, so a given worker may only
ever be driven by one thread; the pool binds a worker to each thread to
keep that invariant. All database access stays on the calling thread.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import db
from .config import Config, Project

# Read these directly; no conversion needed.
TEXT_FAST_PATH = {".txt", ".md", ".markdown"}

# A converted PDF with fewer characters than this is assumed to be a scan.
OCR_TEXT_THRESHOLD = 200

# Upper bound on auto-selected workers. Each one is a Python process that may
# hold a whole workbook or deck in memory, so more is not automatically better.
MAX_AUTO_WORKERS = 8

# Documents converted between database commits. The stage is idempotent, so a
# crash simply re-converts the current group rather than losing anything.
COMMIT_EVERY = 50


def _worker_service() -> None:
    """Runs inside the child process: convert files until stdin closes."""
    import logging
    import os
    import warnings

    out = sys.stdout
    sys.stdout = open(os.devnull, "w")  # libraries print; keep the pipe clean
    warnings.filterwarnings("ignore")
    logging.disable(logging.WARNING)

    converter = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        path = json.loads(line)["path"]
        try:
            if converter is None:
                from markitdown import MarkItDown

                converter = MarkItDown(enable_plugins=False)
            text = converter.convert(path).text_content or ""
            msg = {"status": "ok", "payload": text}
        except Exception as exc:  # markitdown raises many exception types
            msg = {"status": "error", "payload": f"{type(exc).__name__}: {exc}"[:500]}
        out.write(json.dumps(msg) + "\n")
        out.flush()


class ConversionWorker:
    """Drives the conversion child process, enforcing a per-file timeout."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue | None = None

    def _start(self) -> None:
        # Hand the child the exact location of this docpipe package, so it
        # imports correctly no matter how the parent got it on its path.
        env = os.environ.copy()
        pkg_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
        self._proc = subprocess.Popen(
            [sys.executable, "-c",
             "from docpipe.convert import _worker_service; _worker_service()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self._q = queue.Queue()
        threading.Thread(
            target=self._read_lines, args=(self._proc, self._q), daemon=True
        ).start()

    @staticmethod
    def _read_lines(proc: subprocess.Popen, q: queue.Queue) -> None:
        for line in proc.stdout:
            q.put(line)
        q.put(None)  # EOF: the worker exited or crashed

    def convert(self, path: str, timeout: int) -> tuple[str, str]:
        """Returns ("ok", markdown) | ("error", reason) | ("timeout", reason)."""
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        try:
            self._proc.stdin.write(json.dumps({"path": path}) + "\n")
            self._proc.stdin.flush()
        except OSError:
            self._kill()
            return ("error", "the converter crashed on this file")
        try:
            line = self._q.get(timeout=timeout)
        except queue.Empty:
            self._kill()
            return ("timeout", f"took longer than {timeout} seconds")
        if line is None:
            self._kill()
            return ("error", "the converter crashed on this file")
        data = json.loads(line)
        return (data["status"], data["payload"])

    def _kill(self) -> None:
        if self._proc is not None:
            self._proc.kill()
            self._proc.wait()
            self._proc = None
            self._q = None

    def close(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._kill()


class WorkerPool:
    """One ConversionWorker per calling thread.

    A worker's pipe carries an unlabelled request/response pair, so two
    threads sharing one worker would read each other's answers. Binding a
    worker to the thread that first uses it makes that impossible, and costs
    nothing: the pool is only ever driven by a fixed-size executor.
    """

    def __init__(self):
        self._local = threading.local()
        self._workers: list[ConversionWorker] = []
        self._lock = threading.Lock()

    def _worker(self) -> ConversionWorker:
        worker = getattr(self._local, "worker", None)
        if worker is None:
            worker = ConversionWorker()
            self._local.worker = worker
            with self._lock:
                self._workers.append(worker)
        return worker

    def convert(self, path: str, timeout: int) -> tuple[str, str]:
        return self._worker().convert(path, timeout)

    def close(self) -> None:
        with self._lock:
            for worker in self._workers:
                worker.close()
            self._workers.clear()


def resolve_workers(cfg: Config) -> int:
    """How many files to convert at once: config, else one per core, capped."""
    configured = getattr(cfg, "convert_workers", 0) or 0
    if configured > 0:
        return int(configured)
    return max(1, min(MAX_AUTO_WORKERS, os.cpu_count() or 1))


def run(project: Project, cfg: Config, progress=None, workers: int | None = None) -> dict:
    conn = project.connect()
    stats = {"converted": 0, "failed": 0, "needs_ocr": 0}
    pool = WorkerPool()
    timeout = getattr(cfg, "convert_timeout_seconds", 120)
    n_workers = workers if workers and workers > 0 else resolve_workers(cfg)
    try:
        docs = conn.execute(
            "SELECT id, ext, needs_ocr FROM documents WHERE status = 'discovered' ORDER BY id"
        ).fetchall()
        total = len(docs)
        if not total:
            return stats

        # Resolve every path up front: the database belongs to this thread.
        jobs = [(doc, db.first_path(conn, doc["id"])) for doc in docs]

        def convert_one(job):
            """Runs on a pool thread. Touches files only, never the database."""
            doc, path_row = job
            if doc["needs_ocr"]:
                # Images skip conversion entirely; Gemini reads the original.
                return ("ok", "")
            path = Path(path_row["path"])
            if doc["ext"] in TEXT_FAST_PATH:
                try:
                    return ("ok", path.read_text(encoding="utf-8", errors="replace"))
                except OSError as exc:
                    return ("error", str(exc)[:500])
            return pool.convert(str(path), timeout)

        def record(job, result, i):
            doc, path_row = job
            status, payload = result
            if progress:
                progress(f"[{i}/{total}] {path_row['rel_path']}")

            if doc["needs_ocr"]:
                db.update_document(conn, doc["id"], status="converted", markdown="")
                stats["converted"] += 1
                stats["needs_ocr"] += 1
                return

            if status == "ok":
                fields = {"status": "converted", "markdown": payload}
                if doc["ext"] == ".pdf" and len(payload.strip()) < OCR_TEXT_THRESHOLD:
                    fields["needs_ocr"] = 1
                    stats["needs_ocr"] += 1
                db.update_document(conn, doc["id"], **fields)
                stats["converted"] += 1
            elif doc["ext"] == ".pdf":
                # Local conversion failed, but Gemini reads PDFs natively, so
                # route the original file to the model instead of giving up.
                db.update_document(conn, doc["id"], status="converted", markdown="",
                                   needs_ocr=1, convert_error=payload)
                stats["converted"] += 1
                stats["needs_ocr"] += 1
                if progress:
                    progress(f"    could not be read locally ({payload}); "
                             "Gemini will read the original file instead")
            else:
                db.update_document(conn, doc["id"], status="convert_failed",
                                   convert_error=payload)
                stats["failed"] += 1
                if progress:
                    progress(f"    skipped: {payload} (will be listed in the report)")

        if n_workers == 1:
            for i, job in enumerate(jobs, 1):
                record(job, convert_one(job), i)
                if i % COMMIT_EVERY == 0:
                    conn.commit()
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = {executor.submit(convert_one, job): job for job in jobs}
                for i, future in enumerate(as_completed(futures), 1):
                    job = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:  # a bug in convert_one, not in a file
                        result = ("error", f"{type(exc).__name__}: {exc}"[:500])
                    record(job, result, i)
                    if i % COMMIT_EVERY == 0:
                        conn.commit()
        conn.commit()
    finally:
        pool.close()
        conn.close()
    return stats
