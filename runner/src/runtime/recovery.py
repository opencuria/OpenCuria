"""Physical recovery evidence: ownership contradictions and live disk helpers."""

from pathlib import Path

DISK_HELPERS = {
    "qemu-img",
    "virt-customize",
    "guestfish",
    "genisoimage",
    "mkisofs",
}


class RecoveryConflict(RuntimeError):
    """Report contradictory ownership without authorizing resource disposal."""

    code = "publication_ownership_conflict"


def _disk_helper_active(comm: Path) -> bool:
    """Recognize disk helpers still capable of effects, excluding zombie exits."""
    if comm.read_text().strip() not in DISK_HELPERS:
        return False
    # stat is an atomic process observation. Parse after the final name delimiter
    # because process names can themselves contain spaces or parentheses.
    _, separator, status = comm.with_name("stat").read_text().rpartition(") ")
    if not separator or not status:
        return True
    return status.split()[0] not in {"Z", "X", "x"}


def disk_helpers_active() -> bool:
    """Fail closed while crashed-runner disk helpers may still have effects."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return True
    for proc in proc_root.glob("[0-9]*/comm"):
        try:
            if _disk_helper_active(proc):
                return True
        except FileNotFoundError:
            continue  # The process exited between enumeration and inspection.
        except OSError:
            return True  # An unreadable helper is not proven quiescent.
    return False
