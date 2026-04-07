"""
Shared test fixtures for the runners app.
"""

from __future__ import annotations

import os
import uuid

import pytest

from django.contrib.auth import get_user_model

from apps.organizations.models import Organization
from common.utils import generate_api_token, hash_token

from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Session, Workspace

# Allow sync ORM calls from async test functions (Django safety check).
# In production code, we use sync_to_async / async-safe patterns.
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"


@pytest.fixture
def api_token() -> str:
    """A plaintext API token for testing."""
    return generate_api_token()


@pytest.fixture
def organization(db) -> Organization:
    """Default organization for runner/workspace fixtures."""
    return Organization.objects.create(
        name=f"Test Org {uuid.uuid4().hex[:6]}",
        slug=f"test-org-{uuid.uuid4().hex[:10]}",
    )


@pytest.fixture
def user(db):
    """Default user for workspace fixtures."""
    user_model = get_user_model()
    return user_model.objects.create_user(
        email=f"runner-tests-{uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )


@pytest.fixture
def runner(db, api_token: str, organization: Organization) -> Runner:
    """A registered runner in the database."""
    return Runner.objects.create(
        name="test-runner",
        api_token_hash=hash_token(api_token),
        status=RunnerStatus.ONLINE,
        sid="test-sid-123",
        organization=organization,
        available_runtimes=["docker", "qemu"],
    )


@pytest.fixture
def offline_runner(db, organization: Organization) -> Runner:
    """An offline runner in the database."""
    return Runner.objects.create(
        name="offline-runner",
        api_token_hash=hash_token("offline-token"),
        status=RunnerStatus.OFFLINE,
        organization=organization,
        available_runtimes=["docker", "qemu"],
    )


@pytest.fixture
def workspace(runner: Runner, user) -> Workspace:
    """A running workspace in the database."""
    return Workspace.objects.create(
        runner=runner,
        name="Fixture Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )


@pytest.fixture
def stopped_workspace(runner: Runner, user) -> Workspace:
    """A stopped workspace in the database."""
    return Workspace.objects.create(
        runner=runner,
        name="Stopped Fixture Workspace",
        status=WorkspaceStatus.STOPPED,
        created_by=user,
    )


@pytest.fixture
def session(workspace: Workspace) -> Session:
    """A completed session in the database."""
    chat = Chat.objects.create(workspace=workspace, name="Fixture Chat")
    return Session.objects.create(
        chat=chat,
        prompt="Hello, world!",
        output="Response from agent",
        status="completed",
    )
