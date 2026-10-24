from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import mcp.types as mcp_types
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model

from apps.mcp_app.server import create_mcp_server
from apps.mcp_app.tools.image_definitions import (
    _call_create_build_job,
    _call_create_image_definition,
    _call_delete_build_job,
    _call_delete_image_definition,
    _call_duplicate_image_definition,
    _call_get_build_job_log,
    _call_list_build_jobs,
    _call_list_image_definitions,
    _call_update_build_job,
    _call_update_image_definition,
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
        base_distro="ubuntu:24.04",
    )
    ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="Deleted MCP Recipe",
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
        base_distro="ubuntu:24.04",
    )

    payload = _parse_json(
        async_to_sync(_call_delete_image_definition)(
            mcp_image_setup["api_key"],
            mcp_image_setup["org"].id,
            {"definition_id": str(definition.id)},
        )
    )

    definition.refresh_from_db()
    assert payload["status"] == "pending_deletion"
    assert definition.status == ImageDefinition.Status.PENDING_DELETION
    assert ImageDefinition.objects.filter(id=definition.id).exists()


@pytest.mark.django_db(transaction=True)
def test_mcp_delete_build_job_uses_orchestrated_delete(mcp_image_setup):
    definition = ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="MCP Build Delete Recipe",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=mcp_image_setup["runner"],
        status=ImageBuildJob.Status.FAILED,
    )

    payload = _parse_json(
        async_to_sync(_call_delete_build_job)(
            mcp_image_setup["api_key"],
            mcp_image_setup["org"].id,
            {
                "definition_id": str(definition.id),
                "runner_id": str(mcp_image_setup["runner"].id),
            },
        )
    )

    build.refresh_from_db()
    assert payload["phase"] == "waiting_inventory"
    assert build.status == ImageBuildJob.Status.PENDING_DELETION
    assert ImageBuildJob.objects.filter(id=build.id).exists()


def _error_text(result) -> str:
    assert len(result) == 1
    return result[0].text


@pytest.fixture
def mcp_member(mcp_image_setup):
    member = get_user_model().objects.create_user(
        email=f"mcp-image-member-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(
        user=member,
        organization=mcp_image_setup["org"],
        role=MembershipRole.MEMBER,
    )
    return SimpleNamespace(user=member)


@pytest.fixture
def mcp_recipe(mcp_image_setup):
    return ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="MCP Recipe",
        base_distro="ubuntu:24.04",
    )


@pytest.mark.django_db
def test_mcp_create_image_definition(mcp_image_setup):
    payload = _parse_json(
        _call_create_image_definition(
            mcp_image_setup["api_key"],
            mcp_image_setup["org"].id,
            {"name": "  MCP Python  ", "packages": ["python3"]},
        )
    )

    created = ImageDefinition.objects.get(id=payload["id"])
    assert payload == {"id": str(created.id), "name": "MCP Python"}
    assert created.organization_id == mcp_image_setup["org"].id
    assert created.created_by_id == mcp_image_setup["admin"].id
    assert created.base_distro == "ubuntu:22.04"
    assert created.packages == ["python3"]
    assert created.is_active is True


@pytest.mark.django_db
def test_mcp_create_image_definition_validates_input(mcp_image_setup, mcp_member):
    org_id = mcp_image_setup["org"].id
    admin_key = mcp_image_setup["api_key"]
    before = ImageDefinition.objects.count()

    assert (
        _error_text(_call_create_image_definition(admin_key, org_id, {"name": "  "}))
        == "Error: name is required"
    )
    assert (
        _error_text(_call_create_image_definition(mcp_member, org_id, {"name": "Nope"}))
        == "Error: Admin role required"
    )
    assert _error_text(
        _call_create_image_definition(
            admin_key,
            org_id,
            {"name": "VM", "base_distro": "debian:12"},
        )
    ) == (
        "Error: QEMU image definitions currently require an ubuntu:<version> "
        "base distro"
    )
    assert ImageDefinition.objects.count() == before


@pytest.mark.django_db
def test_mcp_duplicate_image_definition_resolves_copy_names(
    mcp_image_setup, mcp_recipe
):
    org_id = mcp_image_setup["org"].id
    api_key = mcp_image_setup["api_key"]
    args = {"definition_id": str(mcp_recipe.id)}

    first = _parse_json(_call_duplicate_image_definition(api_key, org_id, args))
    second = _parse_json(_call_duplicate_image_definition(api_key, org_id, args))
    renamed = _parse_json(
        _call_duplicate_image_definition(api_key, org_id, {**args, "name": " New "})
    )

    assert first["name"] == "MCP Recipe (Copy)"
    assert second["name"] == "MCP Recipe (Copy 2)"
    assert renamed["name"] == "New"
    copied = ImageDefinition.objects.get(id=first["id"])
    assert copied.organization_id == org_id
    assert copied.base_distro == "ubuntu:24.04"


@pytest.mark.django_db
def test_mcp_duplicate_image_definition_errors(mcp_image_setup, mcp_recipe):
    org_id = mcp_image_setup["org"].id
    api_key = mcp_image_setup["api_key"]
    foreign = ImageDefinition.objects.create(
        organization=Organization.objects.create(
            name="Foreign MCP Org", slug=f"foreign-{uuid.uuid4().hex[:8]}"
        ),
        name="Foreign",
    )

    assert (
        _error_text(_call_duplicate_image_definition(api_key, org_id, {}))
        == "Error: definition_id is required"
    )
    assert (
        _error_text(
            _call_duplicate_image_definition(api_key, org_id, {"definition_id": "x"})
        )
        == "Error: Invalid definition_id UUID"
    )
    assert (
        _error_text(
            _call_duplicate_image_definition(
                api_key, org_id, {"definition_id": str(foreign.id)}
            )
        )
        == "Error: Image definition not found"
    )
    assert (
        _error_text(
            _call_duplicate_image_definition(
                api_key, org_id, {"definition_id": str(mcp_recipe.id), "name": " "}
            )
        )
        == "Error: name cannot be empty"
    )


@pytest.mark.django_db
def test_mcp_update_image_definition(mcp_image_setup, mcp_recipe):
    org_id = mcp_image_setup["org"].id
    api_key = mcp_image_setup["api_key"]
    global_recipe = ImageDefinition.objects.create(name="Global MCP", organization=None)

    payload = _parse_json(
        _call_update_image_definition(
            api_key,
            org_id,
            {"definition_id": str(mcp_recipe.id), "description": "Changed"},
        )
    )

    assert payload == {"id": str(mcp_recipe.id), "name": "MCP Recipe"}
    mcp_recipe.refresh_from_db()
    assert mcp_recipe.description == "Changed"
    assert (
        _error_text(
            _call_update_image_definition(
                api_key, org_id, {"definition_id": str(global_recipe.id), "name": "X"}
            )
        )
        == "Error: Image definition not found"
    )
    assert _error_text(
        _call_update_image_definition(
            api_key,
            org_id,
            {
                "definition_id": str(mcp_recipe.id),
                "base_distro": "debian:12",
            },
        )
    ) == (
        "Error: QEMU image definitions currently require an ubuntu:<version> "
        "base distro"
    )
    mcp_recipe.refresh_from_db()


@pytest.mark.django_db
def test_mcp_delete_global_image_definition_is_not_found(mcp_image_setup):
    global_recipe = ImageDefinition.objects.create(name="Global Del", organization=None)

    result = async_to_sync(_call_delete_image_definition)(
        mcp_image_setup["api_key"],
        mcp_image_setup["org"].id,
        {"definition_id": str(global_recipe.id)},
    )

    assert _error_text(result) == "Error: Image definition not found"
    global_recipe.refresh_from_db()
    assert global_recipe.status == ImageDefinition.Status.ACTIVE


@pytest.mark.django_db
def test_mcp_list_build_jobs_payload(mcp_image_setup, mcp_recipe):
    build = ImageBuildJob.objects.create(
        image_definition=mcp_recipe,
        runner=mcp_image_setup["runner"],
        status=ImageBuildJob.Status.FAILED,
        build_log="0123456789",
    )

    payload = _parse_json(
        _call_list_build_jobs(
            mcp_image_setup["api_key"],
            mcp_image_setup["org"].id,
            {"definition_id": str(mcp_recipe.id)},
        )
    )

    assert len(payload) == 1
    assert payload[0]["id"] == str(build.id)
    assert payload[0]["runner_id"] == str(mcp_image_setup["runner"].id)
    assert payload[0]["status"] == "failed"
    assert payload[0]["build_log_size"] == 10
    assert "build_log" not in payload[0]
    assert payload[0]["image_path"] == ""


@pytest.mark.django_db
def test_mcp_create_build_job_scopes_runner_to_org(mcp_image_setup, mcp_recipe):
    foreign_runner = Runner.objects.create(
        name="foreign-mcp-runner",
        api_token_hash=hash_token("foreign-mcp-runner-token"),
        organization=Organization.objects.create(
            name="Foreign Runner Org", slug=f"foreign-runner-{uuid.uuid4().hex[:8]}"
        ),
    )

    result = async_to_sync(_call_create_build_job)(
        mcp_image_setup["api_key"],
        mcp_image_setup["org"].id,
        {"definition_id": str(mcp_recipe.id), "runner_id": str(foreign_runner.id)},
    )

    assert _error_text(result) == "Error: Runner not found"
    assert not ImageBuildJob.objects.filter(image_definition=mcp_recipe).exists()


@pytest.mark.django_db
def test_mcp_update_build_job_actions(mcp_image_setup, mcp_recipe, mcp_member):
    org_id = mcp_image_setup["org"].id
    api_key = mcp_image_setup["api_key"]
    build = ImageBuildJob.objects.create(
        image_definition=mcp_recipe,
        runner=mcp_image_setup["runner"],
        status=ImageBuildJob.Status.ACTIVE,
    )
    args = {
        "definition_id": str(mcp_recipe.id),
        "runner_id": str(mcp_image_setup["runner"].id),
    }

    assert (
        _error_text(
            async_to_sync(_call_update_build_job)(
                api_key, org_id, {**args, "action": "explode"}
            )
        )
        == "Error: action must be one of: deactivate, activate, rebuild"
    )
    assert (
        _error_text(
            async_to_sync(_call_update_build_job)(
                mcp_member, org_id, {**args, "action": "deactivate"}
            )
        )
        == "Error: Admin role required"
    )
    payload = _parse_json(
        async_to_sync(_call_update_build_job)(
            api_key, org_id, {**args, "action": "Deactivate"}
        )
    )

    build.refresh_from_db()
    assert payload == {"id": str(build.id), "status": "deactivated"}
    assert build.status == ImageBuildJob.Status.DEACTIVATED


@pytest.mark.django_db
def test_mcp_get_build_job_log(mcp_image_setup, mcp_recipe, mcp_member):
    ImageBuildJob.objects.create(
        image_definition=mcp_recipe,
        runner=mcp_image_setup["runner"],
        status=ImageBuildJob.Status.FAILED,
        build_log="line\n",
    )
    args = {
        "definition_id": str(mcp_recipe.id),
        "runner_id": str(mcp_image_setup["runner"].id),
    }

    payload = _parse_json(
        _call_get_build_job_log(mcp_member, mcp_image_setup["org"].id, args)
    )
    missing = _call_get_build_job_log(
        mcp_member,
        mcp_image_setup["org"].id,
        {**args, "runner_id": str(uuid.uuid4())},
    )

    assert payload == {"build_log": "line\n"}
    assert _error_text(missing) == "Error: Runner image build not found"


@pytest.mark.django_db(transaction=True)
def test_mcp_dispatcher_runs_async_image_tools_on_its_own_loop(mcp_image_setup):
    """Async recipe tools await the service; no nested loop deadlocks asgiref."""
    definition = ImageDefinition.objects.create(
        organization=mcp_image_setup["org"],
        created_by=mcp_image_setup["admin"],
        name="MCP Dispatcher Recipe",
        base_distro="ubuntu:24.04",
    )
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=mcp_image_setup["runner"],
        status=ImageBuildJob.Status.FAILED,
    )
    api_key = SimpleNamespace(
        user=mcp_image_setup["admin"], has_permission=lambda permission: True
    )
    call_tool = create_mcp_server(api_key).request_handlers[mcp_types.CallToolRequest]

    def dispatch(name: str, arguments: dict) -> object:
        request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )
        result = asyncio.run(call_tool(request))
        return json.loads(result.root.content[0].text)

    build_payload = dispatch(
        "delete_build_job",
        {
            "definition_id": str(definition.id),
            "runner_id": str(mcp_image_setup["runner"].id),
        },
    )
    definition_payload = dispatch(
        "delete_image_definition", {"definition_id": str(definition.id)}
    )

    build.refresh_from_db()
    definition.refresh_from_db()
    assert build_payload["phase"] == "waiting_inventory"
    assert build.status == ImageBuildJob.Status.PENDING_DELETION
    assert definition_payload["status"] == "pending_deletion"
    assert definition.status == ImageDefinition.Status.PENDING_DELETION
