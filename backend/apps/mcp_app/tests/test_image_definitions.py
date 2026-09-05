from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from types import SimpleNamespace

from apps.mcp_app.server import (
    _call_delete_build_job,
    _call_delete_image_definition,
    _call_list_image_definitions,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus
from apps.runners.models import ImageBuildJob, ImageDefinition, Runner
from common.utils import hash_token


def _parse_json(result) -> object:
    assert len(result) == 1
    return json.loads(result[0].text)


@pytest.fixture
def mcp_image_setup(db):
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"MCP Image Org {uuid.uuid4().hex[:6]}",
        slug=f"mcp-image-org-{uuid.uuid4().hex[:8]}",
    )
    admin = user_model.objects.create_user(
        email=f"mcp-image-admin-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="mcp-image-runner",
        api_token_hash=hash_token("mcp-image-runner-token"),
        status=RunnerStatus.ONLINE,
        sid="mcp-image-sid",
        organization=org,
        available_runtimes=["docker"],
    )
    return {
        "org": org,
        "admin": admin,
        "runner": runner,
        "api_key": SimpleNamespace(user=admin),
    }


@pytest.mark.django_db
def test_mcp_list_image_definitions_excludes_deleted(mcp_image_setup):
    visible = ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="Visible MCP Recipe",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )
    ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="Deleted MCP Recipe",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
        status=ImageDefinition.Status.DELETED,
    )

    payload = _parse_json(
        _call_list_image_definitions(
            mcp_image_setup["api_key"],
            mcp_image_setup["org"].id,
            {},
        )
    )

    names = {item["name"]: item for item in payload}
    assert "Deleted MCP Recipe" not in names
    assert names["Visible MCP Recipe"]["status"] == "active"
    assert names["Visible MCP Recipe"]["runner_build_summary"]["active"] == 0
    assert names["Visible MCP Recipe"]["id"] == str(visible.id)


@pytest.mark.django_db(transaction=True)
def test_mcp_delete_image_definition_uses_orchestrated_delete(mcp_image_setup):
    definition = ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="MCP Delete Recipe",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )

    payload = _parse_json(
        _call_delete_image_definition(
            mcp_image_setup["api_key"],
            mcp_image_setup["org"].id,
            {"definition_id": str(definition.id)},
        )
    )

    definition.refresh_from_db()
    assert payload["deleted"] is True
    assert definition.status == ImageDefinition.Status.DELETED
    assert ImageDefinition.objects.filter(id=definition.id).exists()


@pytest.mark.django_db(transaction=True)
def test_mcp_delete_build_job_uses_orchestrated_delete(mcp_image_setup):
    definition = ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="MCP Build Delete Recipe",
        runtime_type="docker",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=mcp_image_setup["runner"],
        status=ImageBuildJob.Status.FAILED,
    )

    payload = _parse_json(
        _call_delete_build_job(
            mcp_image_setup["api_key"],
            mcp_image_setup["org"].id,
            {
                "definition_id": str(definition.id),
                "runner_id": str(mcp_image_setup["runner"].id),
            },
        )
    )

    build.refresh_from_db()
    assert payload["deleted"] is True
    assert build.status == ImageBuildJob.Status.DELETED
    assert ImageBuildJob.objects.filter(id=build.id).exists()
