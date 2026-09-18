"""
Common utility functions.

Rules:
- Generic helpers only
- No trading/business logic
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from core.types import JSONValue, PathLike


def utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.utcnow()


def ensure_directory(path: PathLike) -> Path:
    """Create a directory if it does not exist."""
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_json(path: PathLike) -> JSONValue:
    """Read a JSON file."""
    with Path(path).open("r", encoding="utf-8") as fp:
        return json.load(fp)


def write_json(path: PathLike, data: JSONValue, indent: int = 2) -> None:
    """Write data to a JSON file, atomically (crash-safe).

    BUGFIX (2026-09-18, Phase 3 — see BUG_AUDIT_2026-09-18.md item #10):
    a direct open(path, "w") + json.dump() leaves the file truncated/
    corrupted if the process is killed mid-write (OOM, a CI job
    timeout, a watchdog) — and with no backup, the NEXT load of that
    same file fails outright. paper_trading/virtual_portfolio.py's
    state file and storage/trades/trade_diary.py's per-trade + index
    files are exactly this kind of critical state (a corrupted virtual
    portfolio state file means the WHOLE paper-trading engine can't
    even start).

    Standard fix: write the full content to a temp file in the SAME
    directory (so the final rename is on the same filesystem and
    therefore atomic on POSIX), flush + fsync it, then atomically
    rename it over the real path with os.replace(). A crash at any
    point before the rename leaves the original file completely
    untouched; a crash can never leave a half-written file at `path`.
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(file_path.parent),
        prefix=f".{file_path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=indent, ensure_ascii=False)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, file_path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def sha256_hash(value: str) -> str:
    """Return SHA-256 hash of a string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    """Safely convert a value to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
