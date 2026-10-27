"""Local subprocess runtime for directory-upload integration measurements."""

from __future__ import annotations

import asyncio
import hashlib

from tests.directory_download_runtime import LocalFileRuntime


class LocalUploadRuntime(LocalFileRuntime):
    """Execute containment/mkdir and extract the uploaded tar with real tools."""

    def __init__(self) -> None:
        super().__init__()
        self.archive_sha256 = ""
        self.archive_bytes = 0

    async def put_archive(self, instance_id: str, path: str, content: bytes) -> None:
        """Match the runtime's tar upload using a local extraction subprocess."""
        self.archive_sha256 = hashlib.sha256(content).hexdigest()
        self.archive_bytes = len(content)
        process = await asyncio.create_subprocess_exec(
            "tar",
            "-xf",
            "-",
            "-C",
            path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(content)
        if process.returncode:
            raise RuntimeError(stderr.decode())
