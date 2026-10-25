"""REST contract for image recipe management (``/api/v1/image-definitions/``).

Characterizes status codes, error details and payloads of the recipe and
runner-build endpoints so the service behind REST keeps
both adapters' contracts identical.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    ImageDeletionRequest,
    Runner,
)
from common.utils import hash_token

from .test_image_definitions_api import _auth_headers, _create_api_key

_ALL_IMAGE_PERMISSIONS = [
    APIKeyPermission.IMAGE_DEFINITIONS_READ.value,
    APIKeyPermission.IMAGE_DEFINITIONS_WRITE.value,
    APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS.value,
    APIKeyPermission.IMAGES_DELETE.value,
]


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def recipe_org(db):
    """Org with an admin, a plain member, a runner and an owned recipe."""
    user_model = get_user_model()
    org = Organization.objects.create(name="Recipe Org", slug="recipe-org")
    admin = user_model.objects.create_user(email="recipe-admin@test.com", password="x")
    member = user_model.objects.create_user(
        email="recipe-member@test.com", password="x"
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    Membership.objects.create(user=member, organization=org, role=MembershipRole.MEMBER)
    runner = Runner.objects.create(
        name="recipe-runner",
        api_token_hash=hash_token("recipe-runner-token"),
        status=RunnerStatus.ONLINE,
        organization=org,
    )
    definition = ImageDefinition.objects.create(
        organization=org,
        created_by=admin,
        name="Recipe",
        base_distro="ubuntu:24.04",
    )
    return {
        "org": org,
        "runner": runner,
        "definition": definition,
        "admin_headers": _auth_headers(
            _create_api_key(user=admin, permissions=_ALL_IMAGE_PERMISSIONS),
            str(org.id),
        ),
        "member_headers": _auth_headers(
            _create_api_key(user=member, permissions=_ALL_IMAGE_PERMISSIONS),
            str(org.id),
        ),
    }


def _post(client: Client, url: str, body: dict, headers: dict):
    return client.post(
        url, data=json.dumps(body), content_type="application/json", **headers
    )


def _patch(client: Client, url: str, body: dict, headers: dict):
    return client.patch(
        url, data=json.dumps(body), content_type="application/json", **headers
    )


def test_create_recipe_persists_fields(client, recipe_org):
    response = _post(
        client,
        "/api/v1/image-definitions/",
        {"name": "Python", "packages": ["python3"], "env_vars": {"A": "1"}},
        recipe_org["admin_headers"],
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "Python"
    assert payload["organization_id"] == str(recipe_org["org"].id)
    assert "runtime_type" not in payload
    assert payload["base_distro"] == "ubuntu:22.04"
    assert payload["runner_build_summary"]["active"] == 0
    created = ImageDefinition.objects.get(id=payload["id"])
    assert created.packages == ["python3"]
    assert created.env_vars == {"A": "1"}


def test_recipe_mutations_require_org_admin(client, recipe_org):
    definition = recipe_org["definition"]
    headers = recipe_org["member_headers"]
    recipe_count = ImageDefinition.objects.count()
    responses = [
        _post(client, "/api/v1/image-definitions/", {"name": "x"}, headers),
        _post(
            client,
            f"/api/v1/image-definitions/{definition.id}/duplicate/",
            {},
            headers,
        ),
        _patch(
            client,
            f"/api/v1/image-definitions/{definition.id}/",
            {"name": "y"},
            headers,
        ),
        client.post(
            f"/api/v1/image-definitions/{definition.id}/deactivate/", **headers
        ),
        _post(
            client,
            f"/api/v1/image-definitions/{definition.id}/runner-builds/",
            {"runner_id": str(recipe_org["runner"].id)},
            headers,
        ),
    ]

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {
            "detail": "Admin role required",
            "code": "forbidden",
            "gaps": None,
        }
    definition.refresh_from_db()
    assert definition.name == "Recipe"
    assert definition.status == ImageDefinition.Status.ACTIVE
    assert ImageDefinition.objects.count() == recipe_count


def test_duplicate_recipe_resolves_unique_copy_names(client, recipe_org):
    url = f"/api/v1/image-definitions/{recipe_org['definition'].id}/duplicate/"

    first = _post(client, url, {}, recipe_org["admin_headers"])
    second = _post(client, url, {}, recipe_org["admin_headers"])
    renamed = _post(client, url, {"name": "  Fresh  "}, recipe_org["admin_headers"])

    assert [first.status_code, second.status_code, renamed.status_code] == [
        201,
        201,
        201,
    ]
    assert first.json()["name"] == "Recipe (Copy)"
    assert second.json()["name"] == "Recipe (Copy 2)"
    assert renamed.json()["name"] == "Fresh"


def test_duplicate_recipe_rejects_blank_name_and_unknown_source(client, recipe_org):
    blank = _post(
        client,
        f"/api/v1/image-definitions/{recipe_org['definition'].id}/duplicate/",
        {"name": "   "},
        recipe_org["admin_headers"],
    )
    foreign = ImageDefinition.objects.create(
        organization=Organization.objects.create(name="Foreign", slug="foreign"),
        name="Foreign recipe",
    )
    unknown = _post(
        client,
        f"/api/v1/image-definitions/{foreign.id}/duplicate/",
        {},
        recipe_org["admin_headers"],
    )

    assert blank.status_code == 400
    assert blank.json()["detail"] == "name cannot be empty"
    assert blank.json()["code"] == "validation_error"
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Image definition not found"


def test_update_recipe_changes_only_given_fields(client, recipe_org):
    definition = recipe_org["definition"]

    response = _patch(
        client,
        f"/api/v1/image-definitions/{definition.id}/",
        {"description": "Updated", "packages": ["git"]},
        recipe_org["admin_headers"],
    )

    assert response.status_code == 200
    assert response.json()["description"] == "Updated"
    definition.refresh_from_db()
    assert definition.name == "Recipe"
    assert definition.packages == ["git"]


def test_global_recipe_is_not_editable(client, recipe_org):
    global_definition = ImageDefinition.objects.create(name="Global", organization=None)

    response = _patch(
        client,
        f"/api/v1/image-definitions/{global_definition.id}/",
        {"name": "Hijacked"},
        recipe_org["admin_headers"],
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Image definition not found"
    global_definition.refresh_from_db()
    assert global_definition.name == "Global"


def test_deactivate_and_activate_recipe(client, recipe_org):
    definition = recipe_org["definition"]
    base = f"/api/v1/image-definitions/{definition.id}"

    deactivated = client.post(f"{base}/deactivate/", **recipe_org["admin_headers"])
    activated = client.post(f"{base}/activate/", **recipe_org["admin_headers"])

    assert deactivated.status_code == 200
    assert deactivated.json()["status"] == "deactivated"
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"


def test_assign_runner_rejects_runner_outside_org(client, recipe_org):
    foreign_runner = Runner.objects.create(
        name="foreign-runner",
        api_token_hash=hash_token("foreign-runner-token"),
        organization=Organization.objects.create(name="Other", slug="other"),
    )

    response = _post(
        client,
        f"/api/v1/image-definitions/{recipe_org['definition'].id}/runner-builds/",
        {"runner_id": str(foreign_runner.id)},
        recipe_org["admin_headers"],
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Runner not found"


def test_update_runner_build_rejects_unknown_action(client, recipe_org):
    ImageBuildJob.objects.create(
        image_definition=recipe_org["definition"],
        runner=recipe_org["runner"],
        status=ImageBuildJob.Status.ACTIVE,
    )

    response = _patch(
        client,
        f"/api/v1/image-definitions/{recipe_org['definition'].id}/runner-builds/"
        f"{recipe_org['runner'].id}/",
        {"action": "explode"},
        recipe_org["admin_headers"],
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "action must be one of: deactivate, activate, rebuild"
    )
    assert response.json()["code"] == "conflict"


def test_deactivate_runner_build(client, recipe_org):
    build = ImageBuildJob.objects.create(
        image_definition=recipe_org["definition"],
        runner=recipe_org["runner"],
        status=ImageBuildJob.Status.ACTIVE,
    )

    response = _patch(
        client,
        f"/api/v1/image-definitions/{recipe_org['definition'].id}/runner-builds/"
        f"{recipe_org['runner'].id}/",
        {"action": " Deactivate "},
        recipe_org["admin_headers"],
    )

    assert response.status_code == 200
    assert response.json()["status"] == ImageBuildJob.Status.DEACTIVATED
    build.refresh_from_db()
    assert build.status == ImageBuildJob.Status.DEACTIVATED


@pytest.mark.parametrize("target_type", ["definition", "assignment"])
def test_recipe_deletion_uses_shared_api(client, recipe_org, target_type):
    definition = recipe_org["definition"]
    build = ImageBuildJob.objects.create(
        image_definition=definition,
        runner=recipe_org["runner"],
        status=ImageBuildJob.Status.FAILED,
    )
    target = definition if target_type == "definition" else build
    payload = {"target_type": target_type, "target_id": str(target.id)}
    url = "/api/v1/image-artifacts/deletions/"
    denied = _post(client, url, payload, recipe_org["member_headers"])
    assert denied.status_code == 403
    assert not ImageDeletionRequest.objects.exists()
    response = _post(client, url, payload, recipe_org["admin_headers"])

    assert response.status_code == 200
    assert response.json()["phase"] == "waiting_inventory"
    request = ImageDeletionRequest.objects.get(pk=response.json()["id"])
    assert request.requested_by_id == definition.created_by_id
    target.refresh_from_db()
    assert target.status == "pending_deletion"


def test_runner_build_log_is_scoped_to_visible_builds(client, recipe_org):
    definition = recipe_org["definition"]
    runner = recipe_org["runner"]
    ImageBuildJob.objects.create(
        image_definition=definition,
        runner=runner,
        status=ImageBuildJob.Status.FAILED,
        build_log="step 1\nboom\n",
    )
    other = ImageDefinition.objects.create(organization=recipe_org["org"], name="B")
    foreign = ImageDefinition.objects.create(
        organization=Organization.objects.create(name="Far", slug="far"), name="C"
    )
    headers = recipe_org["member_headers"]

    found = client.get(
        f"/api/v1/image-definitions/{definition.id}/runner-builds/{runner.id}/log/",
        **headers,
    )
    no_build = client.get(
        f"/api/v1/image-definitions/{other.id}/runner-builds/{runner.id}/log/",
        **headers,
    )
    invisible = client.get(
        f"/api/v1/image-definitions/{foreign.id}/runner-builds/{runner.id}/log/",
        **headers,
    )

    assert found.status_code == 200
    assert found.json() == {"build_log": "step 1\nboom\n"}
    assert no_build.status_code == 404
    assert no_build.json()["detail"] == "Runner image build not found"
    assert invisible.status_code == 404
    assert invisible.json()["detail"] == "Image definition not found"


def test_workspace_and_recipe_contracts_expose_vm_configuration_only():
    """Workspace and recipe schemas expose VM configuration without runtime selection."""
    from apps.runners.schemas import (
        ImageDefinitionCreateIn,
        ImageDefinitionUpdateIn,
        RunnerOut,
        WorkspaceCreateIn,
    )

    assert "available_runtimes" not in RunnerOut.model_fields
    assert "qemu_vcpus" in WorkspaceCreateIn.model_fields
    for schema in (WorkspaceCreateIn, ImageDefinitionCreateIn, ImageDefinitionUpdateIn):
        assert "runtime_type" not in schema.model_fields
        assert "custom_dockerfile" not in schema.model_fields
