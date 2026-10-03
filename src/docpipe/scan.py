"""Local pass 1: walk the source tree, hash, deduplicate and register files.

The source folder is treated as strictly read-only. Duplicate content
(same sha256 found at several paths) becomes one document with several
paths, so each unique file is converted and sent to the LLM exactly once.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

from . import db
from .config import IMAGE_EXTENSIONS, JUNK_NAMES, Config, Project


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_junk(name: str) -> bool:
    lower = name.lower()
    return (
        name.startswith(".")
        or name.startswith("~$")  # Office lock files
        or lower in JUNK_NAMES
        or lower.endswith(".tmp")
    )


def run(project: Project, cfg: Config) -> dict:
    source = Path(cfg.source_dir).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"Source folder not found: {source}")

    allowed = {e.lower() for e in cfg.include_extensions}
    image_exts = {e.lower() for e in IMAGE_EXTENSIONS}
    if cfg.include_images:
        allowed |= image_exts
    project_root = project.root.resolve()
    max_bytes = cfg.max_file_mb * 1024 * 1024

    stats = {"seen": 0, "new": 0, "duplicates": 0, "skipped": 0, "already_known": 0}
    conn = project.connect()
    try:
        for dirpath, dirnames, filenames in os.walk(source):
            # Prune hidden directories and the project dir itself (in case it
            # was created inside the source tree).
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and (Path(dirpath) / d).resolve() != project_root
            ]
            for name in sorted(filenames):
                if _is_junk(name):
                    continue
                path = Path(dirpath) / name
                rel_path = str(path.relative_to(source))
                stats["seen"] += 1

                ext = path.suffix.lower()
                try:
                    st = path.stat()
                except OSError as exc:
                    db.add_skipped(conn, str(path), f"stat failed: {exc}")
                    stats["skipped"] += 1
                    continue

                reason = None
                if ext not in allowed:
                    reason = f"extension '{ext or '(none)'}' not included"
                elif st.st_size == 0:
                    reason = "empty file"
                elif st.st_size > max_bytes:
                    reason = f"larger than {cfg.max_file_mb} MB"
                if reason:
                    db.add_skipped(conn, str(path), reason)
                    stats["skipped"] += 1
                    continue

                sha = _sha256(path)
                mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                doc_id, created = db.register_document(
                    conn, sha, name, ext, st.st_size, mtime,
                    needs_ocr=1 if ext in image_exts else 0,
                )
                path_was_new = db.add_path(conn, doc_id, str(path), rel_path)
                if created:
                    stats["new"] += 1
                elif path_was_new:
                    # Same content found at another path: a duplicate.
                    stats["duplicates"] += 1
                else:
                    stats["already_known"] += 1
        conn.commit()
    finally:
        conn.close()
    return stats
