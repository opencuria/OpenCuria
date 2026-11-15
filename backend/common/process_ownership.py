"""Process-lifetime exclusion for the execution worker; API workers are independent."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from pathlib import Path

from django.conf import settings


class ExecutionProcessGuard:
    """Keep a live execution worker exclusive independently of database lease expiry.

    Never unlink the lock file: every contender must lock the same inode. A
    successfully started execution process retains its descriptor until process exit,
    including after worker shutdown while native execution may still drain.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        """Fail immediately while another live process owns this execution data."""
        if self._fd is not None:
            raise RuntimeError(
                "This process already started an execution worker lifetime"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise RuntimeError(
                "Another live execution process owns this execution data; "
                "run exactly one execution worker"
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


def execution_process_lock_path() -> Path:
    """Share a lock with the data identity, without putting secrets in its name."""
    explicit = os.environ.get("EXECUTION_PROCESS_LOCK_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    database = settings.DATABASES["default"]
    name = str(database["NAME"])
    if database["ENGINE"].endswith("sqlite3") and not name.startswith("file:"):
        path = Path(name).expanduser().resolve()
        return path.with_name(path.name + ".execution.lock")
    identity = "|".join(
        str(database.get(key, "")) for key in ("ENGINE", "HOST", "PORT", "NAME")
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f"opencuria-execution-{digest}.lock"
