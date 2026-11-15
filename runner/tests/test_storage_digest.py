"""Publication hashing works on Python 3.10 and rejects unreadable content."""

import hashlib
import io

import pytest
from src.runtime.storage import sha256_digest


@pytest.mark.parametrize("content", [b"", b"publication", b"x" * (3 * 1024 * 1024 + 7)])
def test_stream_digest_matches_sha256_without_unbounded_reads(content):
    class BoundedStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    assert sha256_digest(BoundedStream(content)) == hashlib.sha256(content).hexdigest()


def test_unreadable_stream_does_not_publish_partial_hash():
    class BrokenStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            raise OSError("unreadable publication")

    with pytest.raises(OSError, match="unreadable publication"):
        sha256_digest(BrokenStream(b"image"))
