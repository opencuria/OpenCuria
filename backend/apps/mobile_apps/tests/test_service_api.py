"""Publishing, listing, installing and deleting apps through service and REST."""

from __future__ import annotations

import hashlib

import pytest
from asgiref.sync import async_to_sync

from apps.accounts.models import APIKeyPermission
from apps.mobile_apps.models import App, AppBlob, AppVersion
from apps.mobile_apps.services import VERSION_RETENTION, AppService

from .conftest import PNG, AppWorld, FakeReader, build_files


def publish(world: AppWorld, owner=None, notes: str = "", **build):
    files, package = build_files(**build)
    reader = FakeReader(files, package)
    result = async_to_sync(AppService(reader=reader).publish)(
        owner_id=(owner or world.owner).id,
        organization_id=world.org.id,
        workspace_id=world.workspace.id,
        project_path="/workspace/todo",
        notes=notes,
    )
    assert reader.calls == [(world.workspace.id, "/workspace/todo", "dist")]
    return result


def body(response) -> bytes:
    """Read a streamed file response the way the ASGI handler does."""
    assert response.is_async  # Sync iterators are buffered whole under ASGI.

    async def read() -> bytes:
        return b"".join([chunk async for chunk in response.streaming_content])

    return async_to_sync(read)()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_publishing_twice_adds_a_version_and_dedupes_files(app_world):
    first = publish(app_world, notes="First build")
    second = publish(app_world, version="1.1.0", bundle=b"bundle-android-2")

    assert first.version.app_id == second.version.app_id
    assert (first.version.number, second.version.number) == (1, 2)
    app = App.objects.get(id=first.version.app_id)
    assert (app.name, app.slug, app.project_path) == ("Todo", "todo", "/workspace/todo")
    assert app.workspace_id == app_world.workspace.id
    # bundle-android, bundle-ios, logo, icon + the second android bundle.
    assert AppBlob.objects.count() == 5
    assert second.warnings == ()


def test_slugs_are_scoped_per_owner(app_world):
    mine = publish(app_world)
    theirs = publish(app_world, owner=app_world.stranger)
    assert mine.version.app_id != theirs.version.app_id
    assert theirs.version.number == 1


def test_old_versions_and_their_files_are_pruned(app_world):
    for index in range(VERSION_RETENTION + 2):
        publish(app_world, bundle=f"android-{index}".encode())
    app = App.objects.get()
    numbers = list(app.versions.values_list("number", flat=True))
    assert numbers == list(range(VERSION_RETENTION + 2, 2, -1))
    assert not AppBlob.objects.filter(sha256=sha(b"android-0")).exists()
    assert AppBlob.objects.filter(sha256=sha(b"android-6")).exists()


def test_list_detail_manifest_and_download(app_world):
    published = publish(app_world, notes="Adds dark mode").version
    client = app_world.client()

    listed = client.get("/api/v1/apps/").json()
    assert len(listed) == 1
    latest = listed[0]["latest_version"]
    assert listed[0]["workspace_name"] == "app-dev"
    assert latest["number"] == 1 and latest["notes"] == "Adds dark mode"
    assert latest["icon_sha256"] == sha(PNG)
    assert latest["platforms"] == {
        "android": len(b"bundle-android") + len(b"logo-png"),
        "ios": len(b"bundle-ios") + len(b"logo-png"),
    }

    detail = client.get(f"/api/v1/apps/{published.app_id}/").json()
    assert [version["number"] for version in detail["versions"]] == [1]

    base = f"/api/v1/apps/{published.app_id}/versions/{published.id}"
    manifest = client.get(f"{base}/manifest/?platform=android").json()
    assert manifest["bundle"]["sha256"] == sha(b"bundle-android")
    assert manifest["bundle"]["md5"] == hashlib.md5(b"bundle-android").hexdigest()
    asset = manifest["assets"][0]
    assert asset["key"] == hashlib.md5(b"logo-png").hexdigest()
    assert asset["extension"] == "png"
    assert manifest["expo_config"]["orientation"] == "portrait"
    assert manifest["icon"]["sha256"] == sha(PNG)

    response = client.get(f"{base}/files/{manifest['bundle']['sha256']}/")
    assert response.status_code == 200
    assert body(response) == b"bundle-android"
    assert response["Content-Length"] == str(len(b"bundle-android"))
    assert response["Cache-Control"].endswith("immutable")
    icon = client.get(f"{base}/files/{sha(PNG)}/")
    assert body(icon) == PNG


def test_files_of_other_versions_or_users_are_not_served(app_world):
    first = publish(app_world).version
    second = publish(app_world, bundle=b"bundle-v2").version
    client = app_world.client()
    old_bundle = (
        f"/api/v1/apps/{second.app_id}/versions/{second.id}"
        f"/files/{sha(b'bundle-android')}/"
    )
    assert client.get(old_bundle).status_code == 404
    stranger = app_world.client(user=app_world.stranger)
    assert stranger.get("/api/v1/apps/").json() == []
    for path in (
        f"/api/v1/apps/{first.app_id}/",
        f"/api/v1/apps/{first.app_id}/versions/{first.id}/manifest/?platform=ios",
        f"/api/v1/apps/{first.app_id}/versions/{first.id}/files/{sha(b'bundle-ios')}/",
    ):
        assert stranger.get(path).status_code == 404, path


def test_platform_errors(app_world):
    version = publish(app_world, ios_bundle=None).version
    client = app_world.client()
    base = f"/api/v1/apps/{version.app_id}/versions/{version.id}/manifest/"
    assert client.get(f"{base}?platform=web").status_code == 400
    assert client.get(f"{base}?platform=ios").status_code == 404
    listed = client.get("/api/v1/apps/").json()
    assert set(listed[0]["latest_version"]["platforms"]) == {"android"}


@pytest.mark.parametrize(
    ("permissions", "method", "status"),
    [
        ([], "get", 403),
        ([APIKeyPermission.HARNESS_READ.value], "delete", 403),
    ],
)
def test_api_key_permissions(app_world, permissions, method, status):
    version = publish(app_world).version
    client = app_world.client(permissions=permissions)
    path = f"/api/v1/apps/{version.app_id}/" if method == "delete" else "/api/v1/apps/"
    assert getattr(client, method)(path).status_code == status


def test_delete_removes_versions_and_unshared_files(app_world):
    version = publish(app_world).version
    other = publish(
        app_world, owner=app_world.stranger, bundle=b"bundle-android"
    ).version
    client = app_world.client()

    assert client.delete(f"/api/v1/apps/{version.app_id}/").status_code == 204

    assert not App.objects.filter(id=version.app_id).exists()
    assert not AppVersion.objects.filter(id=version.id).exists()
    # The stranger's identical bundle is still referenced and stays.
    assert AppBlob.objects.filter(sha256=sha(b"bundle-android")).exists()
    assert App.objects.filter(id=other.app_id).exists()
    assert client.delete(f"/api/v1/apps/{version.app_id}/").status_code == 404
