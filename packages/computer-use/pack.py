"""Create the reproducible, reviewed runtime asset shipped with the runner."""

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
output = root.parents[1] / "runner/src/runtime/assets"
output.mkdir(exist_ok=True)
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
    for name in ["server.js", "worker.js", "linux.py", "manifest.json", "NOTICES.txt"]:
        data = (root / "dist" / name).read_bytes()
        entry = tarfile.TarInfo(name)
        entry.size = len(data)
        entry.mode = 0o444
        archive.addfile(entry, io.BytesIO(data))
compressed = io.BytesIO()
with gzip.GzipFile(filename="", fileobj=compressed, mode="wb", mtime=0) as stream:
    stream.write(buffer.getvalue())
asset = compressed.getvalue()
manifest = json.loads((root / "dist/manifest.json").read_text())
version = manifest["version"]
(output / f"computer-use-{version}.tar.gz").write_bytes(asset)
(output / f"computer-use-{version}.json").write_text(
    json.dumps(
        {
            "sha256": hashlib.sha256(asset).hexdigest(),
            "size": len(asset),
            "manifest": manifest,
        },
        indent=2,
    )
    + "\n"
)

(output / f"computer-use-{version}.NOTICES.txt").write_bytes(
    (root / "dist/NOTICES.txt").read_bytes()
)
