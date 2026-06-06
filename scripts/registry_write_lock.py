from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "runtime" / "registry-write.lock"
LOCK_TIMEOUT_SECONDS = 120
LOCK_POLL_SECONDS = 0.1


@contextmanager
def registry_write_lock(timeout_seconds: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize full-registry writes so concurrent commands cannot overwrite each other."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    with open(LOCK_PATH, "a+b") as lock_file:
        while True:
            try:
                _lock_file(lock_file)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待价格库写入锁超时：{LOCK_PATH}") from exc
                time.sleep(LOCK_POLL_SECONDS)
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()).encode("ascii"))
            lock_file.flush()
            yield
        finally:
            _unlock_file(lock_file)


if os.name == "nt":
    import msvcrt

    def _lock_file(lock_file) -> None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(lock_file) -> None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(lock_file) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(lock_file) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
