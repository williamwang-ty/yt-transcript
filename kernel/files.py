"""Small file and clock helpers shared by the pipeline and the long-text executor."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path


def atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    """Write `content` to `path` through a temp file and a rename, so readers never see half a file.

    The file gets `mode`, or by default the mode a plain write would give it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
    tmp_path = Path(tmp_name)
    try:
        if mode is None:
            # mkstemp creates the file 0600; give it the mode a plain write would.
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def now_iso() -> str:
    """Return the current local timestamp in ISO-like wall-clock format."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
