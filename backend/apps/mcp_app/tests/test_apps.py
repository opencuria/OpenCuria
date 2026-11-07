"""MCP app tools: publishing an Expo build to the user's phone and listing apps."""

from __future__ import annotations

import pytest

from apps.mobile_apps import services
from apps.mobile_apps.export import ExportError
from apps.mobile_apps.tests.conftest import FakeReader, build_files
from apps.runners.exceptions import RunnerOfflineError

from .conftest import fails, ok

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


def use_reader(monkeypatch, reader) -> None:
    """Serve builds from *reader* instead of a workspace VM."""
    monkeypatch.setattr(services, "WorkspaceExportReader", lambda: reader)


async def test_publish_then_update_and_list(world, monkeypatch):
    files, package = build_files(
        dependencies={"expo": "~57.0.30", "expo-camera": "~58.0.0"}
    )
    reader = FakeReader(files, package)
    use_reader(monkeypatch, reader)
    ctx = world.ctx()

    first = await ok(ctx, "app_publish", project_path="/workspace/todo", notes="v1")
    assert first["name"] == "Todo"
    assert first["version"] == 1 and first["version_name"] == "1.0.0"
    assert first["platforms"] == ["android", "ios"]
    assert "install it from Apps" in first["next"]
    assert any("expo-camera" in warning for warning in first["warnings"])
    assert reader.calls == [(world.workspace.id, "/workspace/todo", "dist")]

    second = await ok(
        ctx, "app_publish", project_path="/workspace/todo", output_dir="build"
    )
    assert second["app_id"] == first["app_id"]
    assert second["version"] == 2
    assert "update" in second["next"]
    assert reader.calls[-1][2] == "build"

    listed = await ok(ctx, "app_list")
    assert listed["total"] == 1
    app = listed["apps"][0]
    assert app["slug"] == "todo" and app["version"] == 2
    assert app["project_path"] == "/workspace/todo"
    assert app["workspace"] == "billing-api"
    assert listed["runtime"]["expo_sdk"].startswith("57")


async def test_build_problems_reach_the_agent(world, monkeypatch):
    class Failing:
        async def read(self, *args):
            raise ExportError("No dist/metadata.json found. Build the app first.")

    use_reader(monkeypatch, Failing())
    error = await fails(world.ctx(), "app_publish", project_path="todo")
    assert "Build the app first" in error


async def test_offline_runner_is_reported(world, monkeypatch):
    class Offline:
        async def read(self, *args):
            raise RunnerOfflineError("runner-1")

    use_reader(monkeypatch, Offline())
    error = await fails(world.ctx(), "app_publish", project_path="todo")
    assert "Could not read the build from the workspace" in error


async def test_apps_are_private_to_their_owner(world, monkeypatch):
    from asgiref.sync import sync_to_async

    from apps.accounts.models import User
    from apps.organizations.models import Membership, MembershipRole

    files, package = build_files()
    use_reader(monkeypatch, FakeReader(files, package))
    await ok(world.ctx(), "app_publish", project_path="/workspace/todo")

    def stranger():
        user = User.objects.create_user(email="stranger-apps@example.com")
        Membership.objects.create(
            user=user, organization=world.org, role=MembershipRole.MEMBER
        )
        return user

    other = await sync_to_async(stranger)()
    listed = await ok(world.ctx(user=other), "app_list")
    assert listed["apps"] == [] and listed["total"] == 0


async def test_publish_requires_a_project_path(world):
    error = await fails(world.ctx(), "app_publish")
    assert "project_path" in error
