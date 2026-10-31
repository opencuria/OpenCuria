"""A process-lifetime exclusion guard for the single supported ASGI worker."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from pathlib import Path

from django.conf import settings


class BackendProcessGuard:
    """Keep a live backend exclusive independently of database lease expiry.

    Never unlink the lock file: every contender must lock the same inode. A
    successfully started ASGI process retains its descriptor until process exit,
    including after lifespan shutdown while native execution may still drain.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        """Fail immediately while another live process owns this backend data."""
        if self._fd is not None:
            raise RuntimeError("This process already started a backend ASGI lifetime")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RuntimeError(
                "Another live backend process owns this backend data; "
                "run exactly one ASGI worker"
            ) from None
        except BaseException:
            os.close(descriptor)
            raise
        self._fd = descriptor

    def release_failed_start(self) -> None:
        """Release an unsuccessful startup before any native dispatch began."""
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def backend_process_lock_path() -> Path:
    """Share a lock with the data identity, without putting secrets in its name."""
    explicit = os.environ.get("BACKEND_PROCESS_LOCK_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    database = settings.DATABASES["default"]
    name = str(database["NAME"])
    if database["ENGINE"].endswith("sqlite3") and not name.startswith("file:"):
        path = Path(name).expanduser().resolve()
        return path.with_name(path.name + ".asgi.lock")
    identity = "|".join(
        str(database.get(key, "")) for key in ("ENGINE", "HOST", "PORT", "NAME")
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"opencuria-asgi-{digest}.lock"


_process_guard: BackendProcessGuard | None = None


def acquire_backend_process_guard() -> BackendProcessGuard:
    """Retain successful ownership globally until the operating system closes it."""
    global _process_guard
    if _process_guard is None:
        _process_guard = BackendProcessGuard(backend_process_lock_path())
    _process_guard.acquire()
    return _process_guard
