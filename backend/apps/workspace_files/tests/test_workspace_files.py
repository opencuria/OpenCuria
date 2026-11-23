"""Signed links, streaming downloads with ranges, uploads and previews."""

from __future__ import annotations

import gzip
import io
import json
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
from PIL import Image

from apps.accounts.models import APIKeyPermission
from apps.organizations.models import Membership
from apps.workspace_files import services as file_services
from apps.workspace_files import tokens
from apps.workspace_files.http import parse_range

from .conftest import call

pytestmark = pytest.mark.django_db(transaction=True)


def _png(width: int = 300, height: int = 200) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _api(url: str) -> str:
    return f"/api/v1{url}"


# -- Tokens -------------------------------------------------------------------


def test_read_tokens_are_deterministic_scoped_and_expire():
    expiry = tokens.read_link_expiry(now=1000)
    assert expiry == 3600 + 3600
    token = tokens.issue_read_token(
        user_id=7,
        organization_id="org",
        workspace_id="ws",
        path="/workspace/a",
        expires_at=expiry,
    )
    again = tokens.issue_read_token(
        user_id=7,
        organization_id="org",
        workspace_id="ws",
        path="/workspace/a",
        expires_at=expiry,
    )
    assert token == again
    claims = tokens.verify_token(token, "read", now=1000)
    assert claims is not None and claims.path == "/workspace/a" and claims.user_id == 7
    assert tokens.verify_token(token, "upload", now=1000) is None
    assert tokens.verify_token(token, "read", now=expiry) is None
    assert tokens.verify_token(token[:-2] + "xx", "read", now=1000) is None
    assert tokens.verify_token("garbage", "read") is None


def test_upload_tokens_carry_the_exact_file():
    token, expires_at = tokens.issue_upload_token(
        user_id=1,
        organization_id="org",
        workspace_id="ws",
        directory="/workspace/in",
        filename="a.txt",
        size=3,
        conflict="replace",
        now=100,
    )
    assert expires_at == 100 + tokens.UPLOAD_LINK_LIFETIME_SECONDS
    claims = tokens.verify_token(token, "upload", now=100)
    assert (claims.filename, claims.size, claims.conflict) == ("a.txt", 3, "replace")
    assert tokens.verify_token(token, "read", now=100) is None


def test_blob_tokens_name_one_git_object_and_only_stream_it():
    object_id = "a" * 40
    token = tokens.issue_blob_token(
        user_id=3,
        organization_id="org",
        workspace_id="ws",
        path="/workspace/repo/logo.png",
        repo="/workspace/repo",
        object_id=object_id,
        expires_at=5000,
    )
    claims = tokens.verify_token(token, "blob", now=1000)
    assert claims is not None and claims.operation == "blob"
    assert (claims.repo, claims.object_id) == ("/workspace/repo", object_id)
    assert tokens.verify_token(token, "read", now=1000) is None
    assert tokens.verify_token(token, "upload", now=1000) is None
    read = tokens.issue_read_token(
        user_id=3, organization_id="org", workspace_id="ws", path="/a", expires_at=5000
    )
    assert tokens.verify_token(read, "blob", now=1000) is None
    forged = tokens.issue_blob_token(
        user_id=3,
        organization_id="org",
        workspace_id="ws",
        path="/workspace/repo/logo.png",
        repo="/workspace/repo",
        object_id="HEAD",
        expires_at=5000,
    )
    assert tokens.verify_token(forged, "blob", now=1000) is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-99", (0, 99, None)),
        ("bytes=100-", (100, None, None)),
        ("bytes=-500", (None, None, 500)),
        ("bytes=5-1", None),
        ("bytes=0-1,4-5", None),
        ("items=0-1", None),
        ("", None),
    ],
)
def test_range_parsing(header, expected):
    assert parse_range(header) == expected


# -- Links --------------------------------------------------------------------


def test_links_describe_files_folders_and_errors(file_world):
    (file_world.root / "photo.png").write_bytes(_png())
    (file_world.root / "docs").mkdir()
    (file_world.root / "outside").symlink_to("/etc/hostname")
    links = file_world.links(
        "/workspace/photo.png",
        "/workspace/docs",
        "/workspace/missing.txt",
        "/workspace/outside",
        "/etc/passwd",
    )
    photo = links["/workspace/photo.png"]
    assert photo["type"] == "file" and photo["kind"] == "image"
    assert photo["size"] == len(_png())
    assert photo["content_url"].startswith("/workspace-files/content/")
    assert photo["content_url"].endswith(
        f"/photo.png?v={photo['size']}-{photo['modified_at']}"
    )
    assert photo["thumbnail_url"].startswith("/workspace-files/thumbnail/")
    assert links["/workspace/docs"]["download_url"].endswith(
        "docs.tar.gz?v=0-"
        + str(links["/workspace/docs"]["modified_at"])
        + "&download=1"
    )
    assert links["/workspace/docs"]["content_url"] == ""
    assert links["/workspace/missing.txt"]["error"] == "file_not_found"
    assert links["/workspace/outside"]["error"] == "file_not_found"
    assert links["/etc/passwd"]["error"] == "invalid_path"


def test_links_require_permission_and_ownership(file_world):
    url = f"/api/v1/workspaces/{file_world.workspace.id}/files/links/"
    body = json.dumps({"paths": ["/workspace/a"]})
    denied = file_world.client(permissions=[APIKeyPermission.HARNESS_READ.value]).post(
        url, data=body, content_type="application/json"
    )
    assert denied.status_code == 403
    foreign = file_world.client(file_world.stranger).post(
        url, data=body, content_type="application/json"
    )
    assert foreign.status_code == 404
    assert file_world.accessor.commands == []


# -- Downloads ----------------------------------------------------------------


def test_content_streams_whole_files_ranges_and_revalidation(file_world):
    data = bytes(range(256)) * 1000
    (file_world.root / "data.bin").write_bytes(data)
    link = file_world.links("/workspace/data.bin")["/workspace/data.bin"]
    url = _api(link["content_url"])

    status, headers, body = call("GET", url)
    assert status == 200 and body == data
    assert headers["content-length"] == str(len(data))
    assert headers["content-type"] == "application/octet-stream"
    assert headers["content-disposition"].startswith("attachment;")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"].startswith("sandbox")
    assert headers["accept-ranges"] == "bytes"
    assert headers["cache-control"] == "private, max-age=3600"
    assert headers["access-control-allow-origin"] == "*"

    status, headers, body = call("GET", url, headers={"Range": "bytes=1000-1999"})
    assert status == 206 and body == data[1000:2000]
    assert headers["content-range"] == f"bytes 1000-1999/{len(data)}"

    status, _, body = call("GET", url, headers={"Range": "bytes=-10"})
    assert status == 206 and body == data[-10:]

    status, headers, _ = call("GET", url, headers={"Range": f"bytes={len(data)}-"})
    assert status == 416 and headers["content-range"] == f"bytes */{len(data)}"

    etag = headers.get("etag") or call("HEAD", url)[1]["etag"]
    status, _, body = call("GET", url, headers={"If-None-Match": etag})
    assert status == 304 and body == b""

    status, headers, body = call("HEAD", url)
    assert status == 200 and body == b"" and headers["content-length"] == str(len(data))

    stale = url.split("?")[0] + "?v=1-1"
    assert call("GET", stale)[1]["cache-control"] == "private, no-cache"
    assert all(stream.closed for stream in file_world.accessor.streams)


def test_inline_types_and_forced_downloads(file_world):
    (file_world.root / "page.html").write_text("<script>alert(1)</script>")
    (file_world.root / "doc.pdf").write_bytes(b"%PDF-1.4")
    links = file_world.links("/workspace/page.html", "/workspace/doc.pdf")
    status, headers, _ = call("GET", _api(links["/workspace/page.html"]["content_url"]))
    assert status == 200
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert headers["content-disposition"].startswith("inline;")
    _, headers, _ = call("GET", _api(links["/workspace/page.html"]["download_url"]))
    assert headers["content-disposition"].startswith("attachment;")
    _, headers, _ = call("GET", _api(links["/workspace/doc.pdf"]["content_url"]))
    assert headers["content-type"] == "application/pdf"
    assert "content-security-policy" not in headers


def test_folders_download_as_tar_gz(file_world):
    folder = file_world.root / "project"
    (folder / "src").mkdir(parents=True)
    (folder / "src" / "main.py").write_text("print('hi')")
    link = file_world.links("/workspace/project")["/workspace/project"]
    status, headers, body = call("GET", _api(link["download_url"]))
    assert status == 200
    assert headers["content-type"] == "application/gzip"
    assert 'filename="project.tar.gz"' in headers["content-disposition"]
    assert "accept-ranges" not in headers
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(body))) as archive:
        assert "project/src/main.py" in archive.getnames()


def test_links_stop_working_when_access_ends(file_world):
    (file_world.root / "a.txt").write_text("secret")
    url = _api(file_world.links("/workspace/a.txt")["/workspace/a.txt"]["content_url"])
    assert call("GET", url)[0] == 200
    Membership.objects.filter(user=file_world.owner).delete()
    status, _, body = call("GET", url)
    assert status == 404 and b"secret" not in body
    assert call("GET", "/api/v1/workspace-files/content/forged/a.txt")[0] == 404


def test_symlink_swapped_after_signing_is_refused(file_world):
    (file_world.root / "a.txt").write_text("fine")
    url = _api(file_world.links("/workspace/a.txt")["/workspace/a.txt"]["content_url"])
    (file_world.root / "a.txt").unlink()
    (file_world.root / "a.txt").symlink_to("/etc/hostname")
    assert call("GET", url)[0] == 404


def test_oversized_files_are_refused(file_world, monkeypatch):
    monkeypatch.setattr(file_services, "FILE_TRANSFER_MAX_BYTES", 10)
    (file_world.root / "big.bin").write_bytes(b"x" * 11)
    url = _api(
        file_world.links("/workspace/big.bin")["/workspace/big.bin"]["content_url"]
    )
    status, _, body = call("GET", url)
    assert status == 413
    assert json.loads(body)["code"] == "file_too_large"


def test_client_disconnect_stops_the_guest_stream(file_world):
    (file_world.root / "big.bin").write_bytes(b"x" * (4 * 1024 * 1024))
    url = _api(
        file_world.links("/workspace/big.bin")["/workspace/big.bin"]["content_url"]
    )
    status, _, body = call("GET", url, disconnect_after=64 * 1024)
    assert status == 200 and len(body) < 4 * 1024 * 1024
    assert file_world.accessor.streams[-1].closed


def test_busy_workspaces_answer_retry_later(file_world, monkeypatch):
    monkeypatch.setattr(file_services, "STREAMS_PER_WORKSPACE", 0)
    monkeypatch.setattr(file_services, "STREAM_WAIT_SECONDS", 0.01)
    (file_world.root / "a.txt").write_text("x")
    url = _api(file_world.links("/workspace/a.txt")["/workspace/a.txt"]["content_url"])
    status, headers, _ = call("GET", url)
    assert status == 503 and headers["retry-after"] == "2"


# -- Git file sides -----------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@x.test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def git_world(file_world):
    """A repository whose logo differs between commit, index and worktree."""
    repo = file_world.root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    committed, staged = _png(30, 20), _png(60, 20)
    (repo / "assets").mkdir()
    (repo / "assets" / "logo.png").write_bytes(committed)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Logo")
    (repo / "assets" / "logo.png").write_bytes(staged)
    _git(repo, "add", ".")
    (repo / "assets" / "logo.png").unlink()
    file_world.git = file_world.client(
        permissions=[APIKeyPermission.WORKSPACES_GIT_READ.value]
    )
    commit = _git(repo, "rev-parse", "HEAD")
    file_world.blobs = {
        revision: (data, _git(repo, "rev-parse", f"{name}:assets/logo.png"))
        for revision, name, data in (
            ("head", "HEAD", committed),
            (commit, commit, committed),
            ("index", ":0", staged),
        )
    }
    return file_world


def _git_link(world, revision, *, path="assets/logo.png", client=None):
    response = (client or world.git).get(
        f"/api/v1/workspaces/{world.workspace.id}/git/file-link/",
        {"repo_path": "/workspace/repo", "path": path, "revision": revision},
    )
    return response


def test_git_links_stream_index_head_and_commit_blobs(git_world):
    for revision, (data, object_id) in git_world.blobs.items():
        response = _git_link(git_world, revision)
        assert response.status_code == 200, response.content
        link = response.json()["files"][0]
        assert link["path"] == "/workspace/repo/assets/logo.png"
        assert (link["kind"], link["size"]) == ("image", len(data))
        assert link["object_id"] == object_id
        assert link["thumbnail_url"] == ""
        assert link["content_url"].endswith(f"/logo.png?v={link['object_id']}")
        status, headers, body = call("GET", _api(link["content_url"]))
        assert status == 200 and body == data
        assert headers["content-type"] == "image/png"
        assert headers["etag"] == f'"{link["object_id"]}"'
        assert headers["cache-control"] == "private, max-age=3600"
        assert "last-modified" not in headers
    staged = git_world.blobs["index"][0]
    link = _git_link(git_world, "index").json()["files"][0]
    url = _api(link["content_url"])
    status, headers, body = call("GET", url, headers={"Range": "bytes=10-19"})
    assert status == 206 and body == staged[10:20]
    assert headers["content-range"] == f"bytes 10-19/{link['size']}"
    etag = f'"{link["object_id"]}"'
    status, _, body = call("GET", url, headers={"If-None-Match": etag})
    assert status == 304 and body == b""
    # A blob link is no file link: it never previews or reads the worktree.
    thumbnail = url.replace("/content/", "/thumbnail/").split("/logo.png")[0]
    assert call("GET", thumbnail)[0] == 404
    assert all(stream.closed for stream in git_world.accessor.streams)


def test_git_links_keep_streaming_the_signed_blob(git_world):
    link = _git_link(git_world, "index").json()["files"][0]
    repo = git_world.root / "repo"
    (repo / "assets" / "logo.png").write_bytes(b"newer")
    _git(repo, "add", ".")
    status, _, body = call("GET", _api(link["content_url"]))
    assert status == 200 and body == git_world.blobs["index"][0]
    assert _git_link(git_world, "index").json()["files"][0]["size"] == 5


def test_git_links_report_missing_sides_and_worktree_files(git_world):
    deleted = _git_link(git_world, "worktree").json()["files"][0]
    assert deleted["error"] == "file_not_found"
    assert (
        _git_link(git_world, "index", path="nope.png").json()["files"][0]["error"]
        == "file_not_found"
    )
    assert (
        _git_link(git_world, "head", path="assets").json()["files"][0]["error"]
        == "unsupported_file"
    )
    (git_world.root / "repo" / "assets" / "logo.png").write_bytes(b"disk")
    on_disk = _git_link(git_world, "worktree").json()["files"][0]
    assert on_disk["object_id"] == "" and on_disk["size"] == 4
    assert call("GET", _api(on_disk["content_url"]))[2] == b"disk"


def test_git_links_validate_input_permission_and_ownership(git_world):
    for path, revision in (
        ("../secret", "index"),
        ("a", "HEAD:../x"),
        (".git/config", "head"),
    ):
        assert _git_link(git_world, revision, path=path).status_code == 400
    outside = git_world.git.get(
        f"/api/v1/workspaces/{git_world.workspace.id}/git/file-link/",
        {"repo_path": "/etc", "path": "passwd", "revision": "head"},
    )
    assert outside.status_code == 400
    files_only = git_world.client(permissions=[APIKeyPermission.WORKSPACES_READ.value])
    assert _git_link(git_world, "index", client=files_only).status_code == 403
    stranger = git_world.client(
        git_world.stranger, permissions=[APIKeyPermission.WORKSPACES_GIT_READ.value]
    )
    assert _git_link(git_world, "index", client=stranger).status_code == 404


def test_oversized_git_blobs_are_refused(git_world, monkeypatch):
    link = _git_link(git_world, "index").json()["files"][0]
    monkeypatch.setattr(file_services, "FILE_TRANSFER_MAX_BYTES", 10)
    status, _, body = call("GET", _api(link["content_url"]))
    assert status == 413 and json.loads(body)["code"] == "file_too_large"


# -- Uploads ------------------------------------------------------------------


def _upload_url(
    file_world, *, size, filename="a.txt", directory="/workspace/in", conflict="rename"
):
    response = file_world.client().post(
        f"/api/v1/workspaces/{file_world.workspace.id}/files/uploads/",
        data=json.dumps(
            {
                "directory": directory,
                "filename": filename,
                "size": size,
                "conflict": conflict,
            }
        ),
        content_type="application/json",
    )
    return response


def test_uploads_create_files_and_rename_on_conflict(file_world):
    body = b"hello world" * 10_000
    for expected in ("a.txt", "a_1.txt"):
        link = _upload_url(file_world, size=len(body)).json()
        status, _, answer = call(
            "PUT",
            _api(link["upload_url"]),
            headers={"Content-Length": str(len(body))},
            body=body,
        )
        assert status == 201
        assert json.loads(answer)["path"].endswith(f"/in/{expected}")
        assert (file_world.root / "in" / expected).read_bytes() == body
    link = _upload_url(file_world, size=3, conflict="replace").json()
    assert call("PUT", _api(link["upload_url"]), body=b"new")[0] == 201
    assert (file_world.root / "in" / "a.txt").read_bytes() == b"new"


def test_uploads_are_all_or_nothing(file_world):
    link = _upload_url(file_world, size=10).json()
    status, _, answer = call("PUT", _api(link["upload_url"]), body=b"short")
    assert status == 400 and json.loads(answer)["code"] == "upload_incomplete"
    status, _, answer = call("PUT", _api(link["upload_url"]), body=b"x" * 11)
    assert status == 400 and json.loads(answer)["code"] == "size_mismatch"
    leftovers = (
        list((file_world.root / "in").iterdir())
        if (file_world.root / "in").exists()
        else []
    )
    assert [
        p.name for p in leftovers if not p.name.startswith(".opencuria-upload")
    ] == []
    time.sleep(0.2)
    assert not any(
        p.name.startswith(".opencuria-upload") for p in leftovers if p.exists()
    )


def test_upload_links_validate_names_sizes_and_permissions(file_world):
    assert _upload_url(file_world, size=1, filename="../x").status_code == 400
    assert _upload_url(file_world, size=1, directory="/etc").status_code == 400
    too_big = _upload_url(file_world, size=file_services.FILE_TRANSFER_MAX_BYTES + 1)
    assert too_big.status_code == 413
    response = file_world.client(
        permissions=[APIKeyPermission.WORKSPACES_READ.value]
    ).post(
        f"/api/v1/workspaces/{file_world.workspace.id}/files/uploads/",
        data=json.dumps({"directory": "/workspace", "filename": "a", "size": 1}),
        content_type="application/json",
    )
    assert response.status_code == 403
    expired, _ = tokens.issue_upload_token(
        user_id=file_world.owner.id,
        organization_id=str(file_world.org.id),
        workspace_id=str(file_world.workspace.id),
        directory="/workspace",
        filename="a",
        size=1,
        conflict="rename",
        now=0,
    )
    assert call("PUT", f"/api/v1/workspace-files/upload/{expired}", body=b"x")[0] == 404


# -- Thumbnails ---------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_thumbnails_are_tiny_blurred_jpegs_and_cached(file_world):
    (file_world.root / "photo.png").write_bytes(_png())
    link = file_world.links("/workspace/photo.png")["/workspace/photo.png"]
    status, headers, body = call("GET", _api(link["thumbnail_url"]))
    assert status == 200 and headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(body)) as image:
        assert max(image.size) <= file_services.THUMBNAIL_EDGE
        assert image.size[0] > image.size[1]
    opened = len(file_world.accessor.streams)
    assert call("GET", _api(link["thumbnail_url"]))[2] == body
    assert len(file_world.accessor.streams) == opened


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_thumbnails_of_jpegs_and_videos(file_world):
    import subprocess

    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), (30, 30, 200)).save(buffer, format="JPEG")
    (file_world.root / "photo.jpg").write_bytes(buffer.getvalue())
    for name, duration in (("clip.mp4", "3"), ("blip.mp4", "0.2")):
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=duration={duration}:size=320x180:rate=25",
                "-pix_fmt",
                "yuv420p",
                str(file_world.root / name),
            ],
            check=True,
        )
    links = file_world.links(
        "/workspace/photo.jpg", "/workspace/clip.mp4", "/workspace/blip.mp4"
    )
    for path, link in links.items():
        status, _, body = call("GET", _api(link["thumbnail_url"]))
        assert status == 200, path
        with Image.open(io.BytesIO(body)) as image:
            assert image.format == "JPEG" and image.size[0] > image.size[1], path


def test_image_thumbnails_without_ffmpeg_are_rendered_by_the_backend(file_world):
    file_world.accessor.hide_ffmpeg = True
    (file_world.root / "photo.png").write_bytes(_png(120, 240))
    (file_world.root / "clip.mp4").write_bytes(b"not a video")
    links = file_world.links(
        "/workspace/photo.png", "/workspace/clip.mp4", "/workspace/a.txt"
    )
    status, _, body = call("GET", _api(links["/workspace/photo.png"]["thumbnail_url"]))
    assert status == 200
    with Image.open(io.BytesIO(body)) as image:
        assert (
            image.size[1] == file_services.THUMBNAIL_EDGE
            and image.size[0] < image.size[1]
        )
    status, _, body = call("GET", _api(links["/workspace/clip.mp4"]["thumbnail_url"]))
    assert status == 404 and json.loads(body)["code"] == "no_thumbnail"


def test_lifecycle_fences_refuse_transfers_with_conflict(file_world):
    from common.exceptions import ConflictError

    async def fenced(_workspace_id):
        raise ConflictError("Workspace is capturing an image.")

    file_world.service._accessor_factory = fenced
    response = file_world.client().post(
        f"/api/v1/workspaces/{file_world.workspace.id}/files/links/",
        data=json.dumps({"paths": ["/workspace/a.txt"]}),
        content_type="application/json",
    )
    assert response.status_code == 409
    assert "capturing" in response.json()["detail"]
