"""Org-wide subagent config service, REST, and MCP contract tests."""

import json
import uuid
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction
from django.test import Client

from apps.accounts.models import APIKeyPermission
from apps.harness.constants import DEFAULT_MAX_DEPTH, MAX_SUBAGENT_DEPTH
from apps.harness.models import SubagentConfig
from apps.harness.services import SubagentConfigService
from apps.harness.tests.test_agent_s_config import _client
from apps.harness.tests.test_agent_s_config import agent_s_setup as _agent_s_setup
from apps.organizations.models import Organization
from common.exceptions import NotFoundError


@pytest.fixture
def agent_s_setup(db):
    """Reuse the established org/member/outsider setup."""
    return _agent_s_setup.__wrapped__(db)


pytestmark = pytest.mark.django_db
URL = "/api/v1/subagent-config/"
READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]
INVALID = [True, False, 1.0, 2.5, "2", None, 0, -1, MAX_SUBAGENT_DEPTH + 1]


def test_service_defaults_and_upsert(agent_s_setup) -> None:
    """Reads do not write; saves upsert a single org-scoped row."""
    org_id = agent_s_setup["org"].id
    service = SubagentConfigService()
    assert service.get_or_default(org_id) == {"max_depth": DEFAULT_MAX_DEPTH}
    assert not SubagentConfig.objects.exists()
    assert service.save_config(org_id, 1) == {"max_depth": 1}
    row_id = SubagentConfig.objects.get().id
    assert service.save_config(org_id, MAX_SUBAGENT_DEPTH) == {
        "max_depth": MAX_SUBAGENT_DEPTH
    }
    assert SubagentConfig.objects.get().id == row_id
    other = Organization.objects.create(name="Other", slug="other-subagent")
    assert service.get_or_default(other.id) == {"max_depth": DEFAULT_MAX_DEPTH}
    service.save_config(other.id, 3)
    assert service.get_or_default(org_id)["max_depth"] == MAX_SUBAGENT_DEPTH
    assert SubagentConfig.objects.count() == 2


@pytest.mark.parametrize("value", INVALID)
def test_service_rejects_invalid_values(agent_s_setup, value) -> None:
    """Reject coercion and out-of-range values without changing stored data."""
    org_id = agent_s_setup["org"].id
    service = SubagentConfigService()
    with pytest.raises(ValueError, match="max_depth"):
        service.save_config(org_id, value)
    assert not SubagentConfig.objects.exists()
    service.save_config(org_id, 3)
    with pytest.raises(ValueError, match="max_depth"):
        service.save_config(org_id, value)
    assert service.get_or_default(org_id) == {"max_depth": 3}


def test_service_missing_org() -> None:
    """Unknown organizations cannot receive stored config."""
    with pytest.raises(NotFoundError):
        SubagentConfigService().save_config(uuid.uuid4(), 2)
    assert not SubagentConfig.objects.exists()


def test_model_defaults_and_constraint(agent_s_setup) -> None:
    """Model defaults match the service; database rejects zero depth."""
    row = SubagentConfig.objects.create(organization=agent_s_setup["org"])
    assert row.max_depth == DEFAULT_MAX_DEPTH
    with pytest.raises(IntegrityError), transaction.atomic():
        SubagentConfig.objects.filter(pk=row.pk).update(max_depth=0)
    row.refresh_from_db()
    assert row.max_depth == DEFAULT_MAX_DEPTH


def test_rest_roundtrip_and_default_input(agent_s_setup) -> None:
    """REST exposes only max_depth and uses default two for an omitted value."""
    read = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=READ
    )
    run = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=RUN
    )
    assert read.get(URL).json() == {"max_depth": 2}
    assert not SubagentConfig.objects.exists()
    for value in (1, 5, MAX_SUBAGENT_DEPTH):
        response = run.put(
            URL, data=json.dumps({"max_depth": value}), content_type="application/json"
        )
        assert response.status_code == 200, response.content
        assert response.json() == {"max_depth": value}
        assert read.get(URL).json() == response.json()
    response = run.put(URL, data="{}", content_type="application/json")
    assert response.status_code == 200
    assert response.json() == {"max_depth": 2}


@pytest.mark.parametrize("value", INVALID)
def test_rest_validation(agent_s_setup, value) -> None:
    """Schema validation rejects non-integers and bounds before persistence."""
    run = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=RUN
    )
    response = run.put(
        URL, data=json.dumps({"max_depth": value}), content_type="application/json"
    )
    assert response.status_code == 422, response.content
    assert not SubagentConfig.objects.exists()


def test_rest_permissions_and_membership(agent_s_setup) -> None:
    """Read/run permissions, authentication and org membership are enforced."""
    for permissions, get_status, put_status in (
        (READ, 200, 403),
        (RUN, 403, 200),
        ([], 403, 403),
    ):
        client = _client(
            user=agent_s_setup["owner"],
            org=agent_s_setup["org"],
            permissions=permissions,
        )
        assert client.get(URL).status_code == get_status
        assert (
            client.put(URL, data="{}", content_type="application/json").status_code
            == put_status
        )
    outsider = _client(
        user=agent_s_setup["outsider"],
        org=agent_s_setup["org"],
        permissions=READ + RUN,
    )
    assert outsider.get(URL).status_code == 404
    assert (
        outsider.put(URL, data="{}", content_type="application/json").status_code == 404
    )
    anonymous = Client(HTTP_X_ORGANIZATION_ID=str(agent_s_setup["org"].id))
    assert anonymous.get(URL).status_code == 401
    assert (
        anonymous.put(URL, data="{}", content_type="application/json").status_code
        == 401
    )


def test_mcp_parity(agent_s_setup) -> None:
    """MCP metadata, defaults, writes, and membership match REST."""
    from apps.mcp_app.server import (
        _TOOL_HANDLERS,
        _TOOL_PERMISSIONS,
        _TOOLS,
        _call_get_subagent_config,
        _call_save_subagent_config,
    )

    tools = {tool.name: tool for tool in _TOOLS}
    for name, permission in (
        ("get_subagent_config", APIKeyPermission.HARNESS_READ),
        ("save_subagent_config", APIKeyPermission.HARNESS_RUN),
    ):
        assert name in tools
        assert name in _TOOL_HANDLERS
        assert _TOOL_PERMISSIONS[name] == permission
    depth_schema = tools["save_subagent_config"].inputSchema["properties"]["max_depth"]
    assert depth_schema == {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_SUBAGENT_DEPTH,
        "default": 2,
    }
    key = SimpleNamespace(user=agent_s_setup["owner"])
    org_id = agent_s_setup["org"].id
    assert json.loads(_call_get_subagent_config(key, org_id, {})[0].text) == {
        "max_depth": 2
    }
    assert not SubagentConfig.objects.exists()
    saved = _call_save_subagent_config(key, org_id, {"max_depth": 6})
    assert json.loads(saved[0].text) == {"max_depth": 6}
    client = _client(user=key.user, org=agent_s_setup["org"], permissions=READ + RUN)
    assert client.get(URL).json() == {"max_depth": 6}
    client.put(URL, data='{"max_depth": 4}', content_type="application/json")
    assert json.loads(_call_get_subagent_config(key, org_id, {})[0].text) == {
        "max_depth": 4
    }
    for value in INVALID:
        assert _call_save_subagent_config(key, org_id, {"max_depth": value})[
            0
        ].text.startswith("Error:")
        assert SubagentConfigService().get_or_default(org_id) == {"max_depth": 4}
    assert json.loads(_call_save_subagent_config(key, org_id, {})[0].text) == {
        "max_depth": 2
    }
    outsider = SimpleNamespace(user=agent_s_setup["outsider"])
    with pytest.raises(NotFoundError):
        _call_get_subagent_config(outsider, org_id, {})
    with pytest.raises(NotFoundError):
        _call_save_subagent_config(outsider, org_id, {"max_depth": 3})
