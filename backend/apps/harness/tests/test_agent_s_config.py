"""Tests for AgentSConfig service, migration defaults, and REST/MCP parity."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.agent_s.config import AgentSRunConfig
from apps.harness.models import AgentSConfig
from apps.harness.services import AgentSConfigService
from apps.organizations.models import Membership, MembershipRole, Organization
from common.utils import generate_api_token, hash_token


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"agents-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


@pytest.fixture
def agent_s_setup(db):
    """Org + owner + outsider for Agent-S config tests."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"AgentS {uuid.uuid4().hex[:6]}",
        slug=f"agent-s-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"as-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    outsider = user_model.objects.create_user(
        email=f"as-out-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    return {"org": org, "owner": owner, "outsider": outsider}


READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]
URL = "/api/v1/agent-s-config/"


@pytest.mark.django_db(transaction=True)
def test_agent_s_config_defaults(agent_s_setup) -> None:
    """get_or_default returns SDK-aligned defaults without a stored row."""
    service = AgentSConfigService()
    view = service.get_or_default(agent_s_setup["org"].id)
    assert view["grounding_model"] == ""
    assert view["grounding_width"] == 1920
    assert view["grounding_height"] == 1080
    assert view["model_temperature"] is None
    assert view["max_steps"] == 15
    assert view["max_trajectory_length"] == 8
    assert view["enable_reflection"] is True
    assert view["enable_code_agent"] is True
    assert view["screenshot_max_dimension"] == 2400
    assert view["action_pre_delay"] == 1.0
    assert view["action_post_delay"] == 1.0
    assert view["wait_delay"] == 5.0
    assert (
        AgentSConfig.objects.filter(organization_id=agent_s_setup["org"].id).count()
        == 0
    )


@pytest.mark.django_db(transaction=True)
def test_agent_s_config_save_and_grounding_fallback(agent_s_setup) -> None:
    """Save persists fields; to_run_config falls back grounding to main."""
    service = AgentSConfigService()
    view = service.save_config(
        agent_s_setup["org"].id,
        {
            "grounding_model": "",
            "grounding_width": 1000,
            "grounding_height": 1000,
            "model_temperature": None,
            "max_steps": 7,
            "max_trajectory_length": 4,
            "enable_reflection": False,
            "enable_code_agent": False,
            "screenshot_max_dimension": 1200,
            "action_pre_delay": 0.5,
            "action_post_delay": 0.25,
            "wait_delay": 2.0,
        },
    )
    assert view["max_steps"] == 7
    assert view["enable_reflection"] is False
    assert (
        AgentSConfig.objects.filter(organization_id=agent_s_setup["org"].id).count()
        == 1
    )

    run_config = service.to_run_config(
        agent_s_setup["org"].id, main_model="openrouter/acme/main"
    )
    assert isinstance(run_config, AgentSRunConfig)
    assert run_config.main_model == "openrouter/acme/main"
    assert run_config.grounding_model == "openrouter/acme/main"
    assert run_config.grounding_width == 1000
    assert run_config.max_steps == 7
    assert run_config.model_temperature is None

    service.save_config(
        agent_s_setup["org"].id,
        {
            **view,
            "grounding_model": "openai-compatible/uitars",
            "model_temperature": 0.3,
        },
    )
    explicit = service.to_run_config(
        agent_s_setup["org"].id, main_model="openrouter/acme/main"
    )
    assert explicit.grounding_model == "openai-compatible/uitars"
    assert explicit.model_temperature == pytest.approx(0.3)


@pytest.mark.django_db(transaction=True)
def test_agent_s_config_validation(agent_s_setup) -> None:
    """Invalid values raise ValueError (no partial save)."""
    service = AgentSConfigService()
    with pytest.raises(ValueError, match="grounding_width"):
        service.save_config(agent_s_setup["org"].id, {"grounding_width": 0})
    with pytest.raises(ValueError, match="model_temperature"):
        service.save_config(agent_s_setup["org"].id, {"model_temperature": 2.5})
    with pytest.raises(ValueError, match="max_steps"):
        service.save_config(agent_s_setup["org"].id, {"max_steps": 0})
    with pytest.raises(ValueError, match="enable_reflection"):
        service.save_config(agent_s_setup["org"].id, {"enable_reflection": "yes"})
    with pytest.raises(ValueError, match="wait_delay"):
        service.save_config(agent_s_setup["org"].id, {"wait_delay": -1.0})
    assert (
        AgentSConfig.objects.filter(organization_id=agent_s_setup["org"].id).count()
        == 0
    )


@pytest.mark.django_db(transaction=True)
def test_agent_s_config_rest_roundtrip(agent_s_setup) -> None:
    """GET returns defaults; PUT saves; output carries all fields."""
    read_client = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=READ
    )
    get = read_client.get(URL)
    assert get.status_code == 200, get.content[:500]
    body = get.json()
    assert body["grounding_model"] == ""
    assert body["grounding_width"] == 1920
    assert body["model_temperature"] is None
    assert body["max_steps"] == 15

    run_client = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=RUN
    )
    put = run_client.put(
        URL,
        data=json.dumps(
            {
                "grounding_model": "openai-compatible/uitars",
                "grounding_width": 1000,
                "grounding_height": 1000,
                "model_temperature": None,
                "max_steps": 9,
                "max_trajectory_length": 5,
                "enable_reflection": True,
                "enable_code_agent": False,
                "screenshot_max_dimension": 1600,
                "action_pre_delay": 0.2,
                "action_post_delay": 0.3,
                "wait_delay": 1.5,
            }
        ),
        content_type="application/json",
    )
    assert put.status_code == 200, put.content[:500]
    saved = put.json()
    assert saved["grounding_model"] == "openai-compatible/uitars"
    assert saved["max_steps"] == 9
    assert saved["enable_code_agent"] is False
    assert saved["wait_delay"] == 1.5

    again = read_client.get(URL)
    assert again.status_code == 200
    assert again.json()["grounding_model"] == "openai-compatible/uitars"


@pytest.mark.django_db(transaction=True)
def test_agent_s_config_rest_validation_and_auth(agent_s_setup) -> None:
    """PUT validation is 400; permissions and org scoping enforced."""
    run_client = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=RUN
    )
    bad = run_client.put(
        URL,
        data=json.dumps({"max_steps": 0}),
        content_type="application/json",
    )
    assert bad.status_code == 400

    bad_temp = run_client.put(
        URL,
        data=json.dumps({"model_temperature": 3.0}),
        content_type="application/json",
    )
    assert bad_temp.status_code == 400

    read_client = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=READ
    )
    denied = read_client.put(URL, data=json.dumps({}), content_type="application/json")
    assert denied.status_code == 403

    run_only = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=RUN
    )
    denied_get = run_only.get(URL)
    assert denied_get.status_code == 403

    other_org = Organization.objects.create(
        name=f"Other {uuid.uuid4().hex[:6]}",
        slug=f"other-{uuid.uuid4().hex[:10]}",
    )
    outsider_client = _client(
        user=agent_s_setup["outsider"], org=other_org, permissions=READ + RUN
    )
    assert outsider_client.get(URL).status_code == 404

    missing_header = Client(
        HTTP_X_API_KEY=run_client.defaults["HTTP_X_API_KEY"],
    ).get(URL)
    assert missing_header.status_code in (401, 403)


@pytest.mark.django_db(transaction=True)
def test_agent_s_config_mcp_parity(agent_s_setup) -> None:
    """MCP get/save_agent_s_config mirror the REST views."""
    import json as _json
    from types import SimpleNamespace

    from apps.mcp_app.server import (
        _TOOL_HANDLERS,
        _TOOL_PERMISSIONS,
        _TOOLS,
        _call_get_agent_s_config,
        _call_save_agent_s_config,
    )

    names = {tool.name for tool in _TOOLS}
    assert "get_agent_s_config" in names
    assert "save_agent_s_config" in names
    assert "get_agent_s_config" in _TOOL_HANDLERS
    assert "save_agent_s_config" in _TOOL_HANDLERS
    assert _TOOL_PERMISSIONS["get_agent_s_config"] == APIKeyPermission.HARNESS_READ
    assert _TOOL_PERMISSIONS["save_agent_s_config"] == APIKeyPermission.HARNESS_RUN

    api_key = SimpleNamespace(user=agent_s_setup["owner"])
    org_id = agent_s_setup["org"].id
    fetched = _call_get_agent_s_config(api_key, org_id, {})
    assert _json.loads(fetched[0].text)["max_steps"] == 15

    saved = _call_save_agent_s_config(
        api_key,
        org_id,
        {"grounding_model": "openai-compatible/uitars", "max_steps": 6},
    )
    payload = _json.loads(saved[0].text)
    assert payload["grounding_model"] == "openai-compatible/uitars"
    assert payload["max_steps"] == 6
    assert payload["model_temperature"] is None

    invalid = _call_save_agent_s_config(api_key, org_id, {"max_steps": 0})
    assert invalid[0].text.startswith("Error:")


@pytest.mark.django_db(transaction=True)
def test_agent_s_config_rest_then_mcp_partial(agent_s_setup) -> None:
    """REST partial PUT keeps the rest; MCP partial only touches given keys."""
    run_client = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=RUN
    )
    read_client = _client(
        user=agent_s_setup["owner"], org=agent_s_setup["org"], permissions=READ
    )

    seeded = run_client.put(
        URL,
        data=json.dumps(
            {
                "grounding_model": "openai-compatible/uitars",
                "grounding_width": 1000,
                "grounding_height": 1000,
                "model_temperature": None,
                "max_steps": 9,
                "max_trajectory_length": 5,
                "enable_reflection": False,
                "enable_code_agent": False,
                "screenshot_max_dimension": 1600,
                "action_pre_delay": 0.2,
                "action_post_delay": 0.3,
                "wait_delay": 1.5,
            }
        ),
        content_type="application/json",
    )
    assert seeded.status_code == 200, seeded.content[:500]

    partial = run_client.put(
        URL,
        data=json.dumps({"max_steps": 4}),
        content_type="application/json",
    )
    assert partial.status_code == 200, partial.content[:500]
    body = partial.json()
    assert body["max_steps"] == 4
    assert body["grounding_model"] == "openai-compatible/uitars"
    assert body["grounding_width"] == 1000
    assert body["grounding_height"] == 1000
    assert body["max_trajectory_length"] == 5
    assert body["enable_reflection"] is False
    assert body["enable_code_agent"] is False
    assert body["wait_delay"] == 1.5
    assert read_client.get(URL).json()["max_steps"] == 4

    import json as _json
    from types import SimpleNamespace

    from apps.mcp_app.server import _call_save_agent_s_config

    api_key = SimpleNamespace(user=agent_s_setup["owner"])
    saved = _call_save_agent_s_config(
        api_key, agent_s_setup["org"].id, {"wait_delay": 2.5}
    )
    payload = _json.loads(saved[0].text)
    assert payload["wait_delay"] == 2.5
    assert payload["max_steps"] == 4
    assert payload["grounding_model"] == "openai-compatible/uitars"
    assert payload["enable_reflection"] is False
    assert read_client.get(URL).json()["wait_delay"] == 2.5
