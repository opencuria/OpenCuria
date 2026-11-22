"""Host permissions for workspace storage.

Workspace disks, captured images, image caches and seed media hold tenant data
(credentials, SSH keys, repositories), so other local users must not read
them. Files are owner-only. While a domain runs, libvirt's dynamic ownership
hands each of its images (overlay, backing chain, seed ISO) to the QEMU user
and keeps the mode, so QEMU can still open them. Storage directories only let
others traverse to known names, which QEMU needs; nobody else can list them.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

#: Workspace storage files: owner read/write only.
FILE_MODE = 0o600
#: Storage directories: owner full access, others traverse without listing.
DIRECTORY_MODE = 0o711


def ensure_directory(path: Path) -> None:
    """Create *path* and missing parents as storage directories.

    Every directory created here and *path* itself get :data:`DIRECTORY_MODE`
    regardless of the process umask. Existing parents keep their mode.
    """
    missing: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=DIRECTORY_MODE)
        except FileExistsError:
            continue
        directory.chmod(DIRECTORY_MODE)
    path.chmod(DIRECTORY_MODE)


def restrict_tree(root: Path) -> int:
    """Remove group and other access below *root*; return changed entries.

    Files keep owner bits only. Directories keep only traversal for others,
    so QEMU can still reach images libvirt hands it. Symbolic links are
    neither followed nor changed. Entries the runner cannot change (for
    example on hosts where it does not run as root) are logged, not fatal.
    """
    changed = 0
    for directory, subdirectories, files in os.walk(root):
        for name, keep in [(n, 0o711) for n in subdirectories] + [
            (n, 0o700) for n in files
        ]:
            changed += _restrict(Path(directory, name), keep)
    return changed


def _restrict(path: Path, keep: int) -> int:
    """Clear permission bits outside *keep* on one regular file or directory."""
    try:
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            return 0
        mode = stat.S_IMODE(info.st_mode)
        if not mode & ~keep & 0o777:
            return 0
        path.chmod(mode & (keep | ~0o777))
        return 1
    except FileNotFoundError:
        return 0
    except PermissionError:
        logger.warning("host_storage_permissions_not_restricted", path=str(path))
        return 0
