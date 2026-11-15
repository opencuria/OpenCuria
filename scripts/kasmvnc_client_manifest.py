"""Print a pinned desktop web client manifest served by the desktop proxy.

The backend serves only files listed here, and only when the bytes the
desktop upstream returns match the recorded SHA-256. Two clients exist:

* ``kasmvnc``: the KasmVNC client of Linux guests. Regenerate it whenever the
  KasmVNC version in ``backend/apps/runners/scripts/qemu_desktop_session.sh``
  changes, from the official release package::

    dpkg-deb -x kasmvncserver_jammy_<version>_amd64.deb /tmp/kasmvnc
    python3 scripts/kasmvnc_client_manifest.py <version> \\
        /tmp/kasmvnc/usr/share/kasmvnc/www \\
        > backend/apps/runners/kasmvnc_client.json

* ``novnc``: the noVNC client of Windows guests, whose display QEMU serves.
  Regenerate it when ``runner/src/runtime/assets/novnc-<version>.tar.gz``
  changes::

    tar -xzf runner/src/runtime/assets/novnc-<version>.tar.gz -C /tmp
    python3 scripts/kasmvnc_client_manifest.py --client novnc <version> \\
        /tmp/noVNC-<version> > backend/apps/runners/novnc_client.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# What each client loads at runtime; sources, tests, docs and po files are not.
_PATTERNS = {
    "kasmvnc": (
        "index.html",
        "package.json",
        "vendor/interact.min.js",
        "dist/**/*",
        "app/images/**/*",
        "app/sounds/*",
        "app/locale/*.json",
    ),
    "novnc": (
        "vnc.html",
        "defaults.json",
        "mandatory.json",
        "app/**/*",
        "core/**/*",
        "vendor/**/*",
    ),
}
_EXTENSIONS = {
    ".css", ".gif", ".html", ".jpg", ".js", ".json", ".mp3", ".oga", ".png",
    ".svg", ".ttf", ".woff",
}


def build_manifest(version: str, root: Path, client: str = "kasmvnc") -> dict[str, object]:
    """Return the manifest of runtime client files below *root*."""
    files: dict[str, str] = {}
    for pattern in _PATTERNS[client]:
        for path in sorted(root.glob(pattern)):
            if path.is_file() and path.suffix in _EXTENSIONS:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                files[path.relative_to(root).as_posix()] = digest
    if client == "kasmvnc":
        # KasmVNC ships vnc.html as a symlink to index.html.
        files["vnc.html"] = files["index.html"]
    return {"version": version, "files": dict(sorted(files.items()))}


def main() -> None:
    """Parse arguments and print the manifest as JSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client", choices=sorted(_PATTERNS), default="kasmvnc")
    parser.add_argument("version", help="Client release, e.g. 1.3.3")
    parser.add_argument("www", type=Path, help="Extracted client directory")
    args = parser.parse_args()
    json.dump(build_manifest(args.version, args.www, args.client), sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
