"""PUSH_PROXY_URL is the only push setting; no provider credentials remain."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.credentials.models import CredentialService
from apps.credentials.services import CredentialServiceSvc, CredentialSvc
from apps.organizations.models import Membership, Organization

from ..configuration import get_push_settings
from ..models import PushDelivery
from ..proxy import DeliveryResult
from ..services import NotificationService
from .conftest import PROXY_URL, registration


@pytest.mark.django_db
def test_capabilities_name_the_proxy_and_require_membership(client, setup, headers):
    org, user, *_ = setup
    Membership.objects.filter(user=user, organization=org).update(role="member")
    other = Organization.objects.create(name="Another tenant", slug="another-tenant")
    Membership.objects.create(user=user, organization=other, role="member")
    path = "/api/v1/notifications/capabilities/"
    expected = {
        "proxy_url": PROXY_URL,
        "server_key": get_push_settings().identity.public_key,
    }

    assert client.get(path).status_code == 401
    assert client.get(path, **headers).json() == expected
    assert (
        client.get(path, **{**headers, "HTTP_X_ORGANIZATION_ID": str(other.id)}).json()
        == expected
    )
    Membership.objects.filter(user=user, organization=other).delete()
    assert (
        client.get(
            path, **{**headers, "HTTP_X_ORGANIZATION_ID": str(other.id)}
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_empty_proxy_url_turns_mobile_push_off(client, setup, headers, settings):
    settings.PUSH_PROXY_URL = ""
    response = client.get("/api/v1/notifications/capabilities/", **headers)
    assert response.json() == {"proxy_url": None, "server_key": None}


@pytest.mark.parametrize(
    "value",
    [
        "http://push.example.com",
        "push.example.com",
        "https://push.example.com/v1",
        "https://user@push.example.com",
        "https://push.example.com:99999",
        "ftp://push.example.com",
    ],
)
def test_invalid_proxy_urls_fail_startup(settings, value):
    settings.PUSH_PROXY_URL = value
    with pytest.raises(ImproperlyConfigured, match="PUSH_PROXY_URL"):
        apps.get_app_config("notifications").ready()


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://push.opencuria.dev/", "https://push.opencuria.dev"),
        ("HTTPS://Push.OpenCuria.dev", "https://push.opencuria.dev"),
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090"),
        ("https://push.example.com:443", "https://push.example.com"),
        ("https://push.example.com:8443/", "https://push.example.com:8443"),
        ("http://[::1]:80", "http://[::1]"),
        ("  ", None),
    ],
)
def test_proxy_url_is_normalized(settings, value, expected):
    settings.PUSH_PROXY_URL = value
    assert get_push_settings().proxy_url == expected


def test_default_proxy_is_the_public_opencuria_proxy():
    from django.conf import settings

    assert settings.PUSH_PROXY_URL == "https://push.opencuria.dev"


@pytest.mark.django_db
def test_organization_configuration_api_is_removed(client, setup, headers):
    path = "/api/v1/notifications/configuration/"
    assert client.get(path, **headers).status_code == 404


@pytest.mark.django_db
def test_push_never_creates_credentials_or_workspace_material(setup):
    org, user, workspace, *_ = setup
    assert not CredentialService.objects.filter(
        slug__in=["mobile-push-apns", "mobile-push-fcm"]
    ).exists()
    assert not CredentialSvc().list_credentials(user, org.id)
    resolved = CredentialSvc().resolve_workspace_credentials(workspace)
    assert not resolved.env_vars and not resolved.files and not resolved.ssh_keys
    with pytest.raises(ValueError, match="Invalid credential type"):
        CredentialServiceSvc().create_service(
            name="Retired server type",
            slug="retired-server-type",
            description="",
            credential_type="server_secret",
            env_var_name="",
            target_path="",
            label="",
            organization_id=org.id,
        )


@pytest.mark.django_db
def test_registration_is_refused_while_push_is_off(setup, device_keys, settings):
    org, user, *_ = setup
    settings.PUSH_PROXY_URL = ""
    with pytest.raises(ValueError, match="turned off"):
        NotificationService().register(
            user, org.id, uuid.uuid4(), registration(device_keys)
        )


@pytest.mark.django_db(transaction=True)
def test_turning_push_off_stops_queued_delivery(setup, device, settings):
    message = setup[4]
    message.completed_at = timezone.now()
    message.save()
    NotificationService().record_finished(message.id)
    job = PushDelivery.objects.get(kind="alert")
    settings.PUSH_PROXY_URL = ""
    proxy = type("Proxy", (), {"send": AsyncMock()})()
    assert asyncio.run(NotificationService(proxy=proxy).deliver(job)) == (
        DeliveryResult("failed", "proxy_disabled")
    )
    proxy.send.assert_not_called()
