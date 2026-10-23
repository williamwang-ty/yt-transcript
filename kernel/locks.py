"""File locks shared by every `run` process on this machine (flock).

A lock is an flock on a file that is never deleted: deleting a held lock file
lets the next process lock a new file under the same name, and then two
processes hold "the" lock. Waiting is the kernel's queue, with no polling. A
process that exits or is killed releases its locks at once; a child process
that inherited the descriptor keeps the lock until it exits too. The holder
writes who it is into the file, so a waiter can say whom it waits for.

The wait is bounded with SIGALRM, so `acquire` must run in the main thread.

Locks about the whole machine (YouTube, the articles folder) live in one fixed
directory, whatever the work root: `machine_dir()`.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
from datetime import datetime
from pathlib import Path

# The machine-wide lock directory; tests point it elsewhere.
LOCK_DIR_ENV = "YT_TRANSCRIPT_LOCK_DIR"
DEFAULT_LOCK_DIR = "/tmp/yt-transcript/.locks"


def machine_dir() -> Path:
    return Path(os.environ.get(LOCK_DIR_ENV) or DEFAULT_LOCK_DIR).expanduser()


class Busy(Exception):
    """The lock stayed taken for the whole wait."""

    def __init__(self, path: Path, holder: dict):
        super().__init__(f"{path} is held by {holder or 'another process'}")
        self.path = path
        self.holder = holder


class _Timeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise _Timeout()


def read_holder(path: Path) -> dict:
    """What the last holder wrote into the lock file; {} when nothing readable is there."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def is_held(path: Path) -> bool:
    """Whether some process holds the lock right now."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def describe(holder: dict) -> str:
    """'pid 123 (video abc, since 2026-10-23T10:00:00)' for messages."""
    details = [f"{key} {holder[key]}" for key in ("video_id",) if holder.get(key)]
    if holder.get("since"):
        details.append(f"since {holder['since']}")
    pid = f"pid {holder['pid']}" if holder.get("pid") else "another process"
    return f"{pid} ({', '.join(details)})" if details else pid


class FileLock:
    """An exclusive flock on `path`, held from `acquire` until `release` or the end of the process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def acquire(self, timeout: float | None = None, on_wait=None, **record) -> None:
        """Take the lock, waiting at most `timeout` seconds (None: as long as it takes).

        Raises Busy when the lock stays taken. `on_wait(holder)` is called once
        before waiting. `record` is written into the lock file with the pid.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if timeout is not None and timeout <= 0:
                    raise Busy(self.path, read_holder(self.path)) from None
                if on_wait:
                    on_wait(read_holder(self.path))
                self._wait(fd, timeout)
        except BaseException:
            os.close(fd)
            raise
        payload = {"pid": os.getpid(), "since": datetime.now().astimezone().isoformat(timespec="seconds"), **record}
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"), 0)
        self.fd = fd

    def _wait(self, fd: int, timeout: float | None) -> None:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return
        previous = signal.signal(signal.SIGALRM, _on_alarm)
        try:
            try:
                signal.setitimer(signal.ITIMER_REAL, timeout)
                fcntl.flock(fd, fcntl.LOCK_EX)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        except _Timeout:
            # Also raised when the lock came through just as the alarm fired; closing the fd gives it back.
            raise Busy(self.path, read_holder(self.path)) from None
        finally:
            signal.signal(signal.SIGALRM, previous)

    def release(self) -> None:
        """Close the descriptor; the lock is free once no child process still has it."""
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)
