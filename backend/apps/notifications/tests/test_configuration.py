"""Hosting settings replace all organization configuration and vault references."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.credentials.models import CredentialService
from apps.credentials.services import CredentialServiceSvc, CredentialSvc
from apps.organizations.models import Membership, Organization

from ..configuration import get_push_settings
from ..models import PushDelivery
from ..providers import DeliveryResult, PushProviders
from ..services import NotificationService


@pytest.mark.django_db
def test_capabilities_are_server_wide_but_require_membership(client, setup, headers):
    org, user, *_ = setup
    Membership.objects.filter(user=user, organization=org).update(role="member")
    other = Organization.objects.create(name="Another tenant", slug="another-tenant")
    Membership.objects.create(user=user, organization=other, role="member")
    path = "/api/v1/notifications/capabilities/"
    expected = {"ios": True, "android": True, "live_activities": True}

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
def test_organization_configuration_api_is_removed(client, setup, headers):
    path = "/api/v1/notifications/configuration/"
    assert client.get(path, **headers).status_code == 404
    assert (
        client.put(
            path, data="{}", content_type="application/json", **headers
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_push_settings_never_create_credentials_or_workspace_material(setup):
    org, user, workspace, *_ = setup
    assert not CredentialService.objects.filter(
        slug__in=["mobile-push-apns", "mobile-push-fcm"]
    ).exists()
    assert not CredentialSvc().list_credentials(user, org.id)
    assert not workspace.credentials.exists()
    resolved = CredentialSvc().resolve_workspace_credentials(workspace)
    assert not resolved.env_vars
    assert not resolved.files
    assert not resolved.ssh_keys
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


@pytest.mark.parametrize("apns,fcm", [(False, False), (True, False), (False, True)])
def test_providers_are_optional_independently(push_settings, apns, fcm):
    if not apns:
        push_settings.APNS_TEAM_ID = ""
        push_settings.APNS_KEY_ID = ""
        push_settings.APNS_PRIVATE_KEY_FILE = ""
    if not fcm:
        push_settings.FCM_SERVICE_ACCOUNT_FILE = ""
    assert get_push_settings().capabilities() == {
        "ios": apns,
        "android": fcm,
        "live_activities": apns,
    }


@pytest.mark.parametrize(
    "missing", ["APNS_TEAM_ID", "APNS_KEY_ID", "APNS_PRIVATE_KEY_FILE"]
)
def test_apns_settings_must_be_complete(push_settings, missing):
    setattr(push_settings, missing, "")
    with pytest.raises(ImproperlyConfigured, match="must be set together"):
        apps.get_app_config("notifications").ready()


@pytest.mark.parametrize(
    "provider,failure",
    [
        ("apns", "team_id"),
        ("apns", "key_id"),
        ("apns", "key"),
        ("apns", "key_type"),
        ("apns", "json"),
        ("apns", "empty"),
        ("fcm", "json"),
        ("fcm", "fields"),
        ("fcm", "key"),
        ("fcm", "key_type"),
        ("fcm", "empty"),
    ],
)
def test_invalid_hosting_files_fail_startup_without_disclosing_secrets(
    push_settings, push_keys, provider, failure
):
    if provider == "apns":
        path = Path(push_settings.APNS_PRIVATE_KEY_FILE)
        if failure in {"team_id", "key_id"}:
            setattr(push_settings, f"APNS_{failure.upper()}", "private-sentinel")
        elif failure == "key_type":
            path.write_text(push_keys["fcm"]["private_key"], encoding="utf-8")
        elif failure == "json":
            path.write_text(json.dumps(push_keys["apns"]), encoding="utf-8")
        else:
            path.write_text("" if failure == "empty" else "private-sentinel")
        name = "APNS_PRIVATE_KEY_FILE"
    else:
        path = Path(push_settings.FCM_SERVICE_ACCOUNT_FILE)
        data = {**push_keys["fcm"]}
        if failure == "fields":
            data.pop("project_id")
        elif failure == "key":
            data["private_key"] = "private-sentinel"
        elif failure == "key_type":
            data["private_key"] = push_keys["apns"]["private_key"]
        path.write_text(
            "private-sentinel"
            if failure == "json"
            else ""
            if failure == "empty"
            else json.dumps(data),
            encoding="utf-8",
        )
        name = "FCM_SERVICE_ACCOUNT_FILE"
    with pytest.raises(ImproperlyConfigured, match=name) as error:
        apps.get_app_config("notifications").ready()
    assert "private-sentinel" not in str(error.value)
    assert "BEGIN PRIVATE KEY" not in str(error.value)


@pytest.mark.parametrize("name", ["APNS_PRIVATE_KEY_FILE", "FCM_SERVICE_ACCOUNT_FILE"])
@pytest.mark.parametrize(
    "failure", ["missing", "directory", "utf8", "relative", "inline"]
)
def test_key_files_require_readable_absolute_paths(
    push_settings, push_keys, tmp_path, name, failure
):
    path = tmp_path / "private-sentinel"
    if failure == "directory":
        path.mkdir()
    elif failure == "utf8":
        path.write_bytes(b"\xff\xfe")
    value = str(path)
    if failure == "relative":
        value = "private-sentinel"
    elif failure == "inline":
        value = (
            push_keys["apns"]["private_key"]
            if name == "APNS_PRIVATE_KEY_FILE"
            else json.dumps(push_keys["fcm"])
        )
    setattr(push_settings, name, value)
    with pytest.raises(ImproperlyConfigured, match=name) as error:
        apps.get_app_config("notifications").ready()
    assert "private-sentinel" not in str(error.value)
    assert "BEGIN PRIVATE KEY" not in str(error.value)


@pytest.mark.parametrize("name", ["APNS_PRIVATE_KEY_FILE", "FCM_SERVICE_ACCOUNT_FILE"])
def test_unreadable_key_files_fail_startup(push_settings, monkeypatch, name):
    read_text = Path.read_text

    def denied(path, *args, **kwargs):
        if str(path) == getattr(push_settings, name):
            raise PermissionError("private-sentinel")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(ImproperlyConfigured, match=f"{name} must point to a readable"):
        apps.get_app_config("notifications").ready()


def test_apns_requires_the_correct_ec_curve(push_settings):
    key = (
        ec.generate_private_key(ec.SECP384R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    Path(push_settings.APNS_PRIVATE_KEY_FILE).write_text(key, encoding="utf-8")
    with pytest.raises(ImproperlyConfigured, match="APNS_PRIVATE_KEY_FILE"):
        get_push_settings()


def test_key_files_are_loaded_at_startup_only(push_settings, monkeypatch):
    apps.get_app_config("notifications").ready()

    def unexpected_read(*args, **kwargs):
        raise AssertionError("Push files were read after startup")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    assert get_push_settings().capabilities() == {
        "ios": True,
        "android": True,
        "live_activities": True,
    }


@pytest.mark.django_db(transaction=True)
def test_disabling_server_provider_stops_previously_queued_delivery(
    setup, device, settings
):
    message = setup[4]
    message.completed_at = timezone.now()
    message.save()
    NotificationService().record_finished(message.id)
    job = PushDelivery.objects.get(kind="alert")
    settings.APNS_TEAM_ID = ""
    settings.APNS_KEY_ID = ""
    settings.APNS_PRIVATE_KEY_FILE = ""
    providers = type("Providers", (), {"send_apns": AsyncMock()})()
    assert asyncio.run(NotificationService(providers=providers).deliver(job)) == (
        DeliveryResult("failed", "provider_not_configured")
    )
    providers.send_apns.assert_not_called()


def test_disabled_providers_never_contact_apple_or_google(settings):
    settings.APNS_TEAM_ID = ""
    settings.APNS_KEY_ID = ""
    settings.APNS_PRIVATE_KEY_FILE = ""
    settings.FCM_SERVICE_ACCOUNT_FILE = ""

    def unexpected_io(request):
        raise AssertionError("Disabled push provider performed I/O")

    async def run():
        providers = PushProviders(
            httpx.AsyncClient(transport=httpx.MockTransport(unexpected_io))
        )
        expected = DeliveryResult("failed", "provider_not_configured")
        assert (
            await providers.send_apns(
                token="ab" * 32,
                app_id="com.opencuria.mobile",
                environment="production",
                payload={"aps": {}},
                collapse_id="test",
            )
            == expected
        )
        assert (
            await providers.send_fcm(
                token="fcm-token",
                title="Test",
                body="Test",
                data={},
                collapse_id="test",
                app_id="com.opencuria.mobile",
            )
            == expected
        )
        await providers.close()

    asyncio.run(run())
