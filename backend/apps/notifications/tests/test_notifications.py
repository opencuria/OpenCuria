"""Native delivery contracts: ownership, lifecycle, restart and provider failures."""

import asyncio
import json
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from django.utils import timezone

from apps.accounts.auth_backends import DjangoJWTBackend
from apps.accounts.models import User
from apps.harness.conversation_activity_service import ConversationActivityService
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessSession, QuestionRequest
from apps.harness.permissions.models import PermissionRequest
from apps.harness.run_events import publish_assistant_run_finished
from apps.organizations.models import Membership, Organization
from common.utils import decrypt_value

from ..models import MobileDevice, PushConfiguration, PushDelivery
from ..providers import DeliveryResult
from ..repositories import NotificationRepository
from ..schemas import ActivityRegistrationIn, DeviceRegistrationIn, PushConfigurationIn
from ..services import NotificationService

pytestmark = pytest.mark.django_db


def finish(message, reason="stop", error=""):
    message.finish = reason
    message.error = error
    message.completed_at = timezone.now()
    message.save(update_fields=["finish", "error", "completed_at"])


def providers(result=DeliveryResult("sent")):
    class Providers:
        send_apns = AsyncMock(return_value=result)
        send_fcm = AsyncMock(return_value=result)

    return Providers()


def test_device_api_enforces_scope_and_never_echoes_tokens(client, setup, headers):
    org, user, *_ = setup
    device_id = uuid.uuid4()
    payload = {
        "platform": "ios",
        "app_id": "com.opencuria.mobile",
        "push_token": "cd" * 32,
        "notifications_enabled": True,
    }
    path = f"/api/v1/notifications/devices/{device_id}/"
    assert (
        client.put(
            path, data=json.dumps(payload), content_type="application/json"
        ).status_code
        == 401
    )
    response = client.put(
        path, data=json.dumps(payload), content_type="application/json", **headers
    )
    assert response.status_code == 200
    assert set(response.json()) == {
        "id",
        "notifications_enabled",
        "live_activities_enabled",
    }
    device = MobileDevice.objects.get(id=device_id)
    assert decrypt_value(device.encrypted_push_token) == payload["push_token"]
    assert payload["push_token"] not in device.encrypted_push_token
    outsider = User.objects.create_user(email="outsider@example.com")
    Membership.objects.create(user=outsider, organization=org)
    token = DjangoJWTBackend().generate_tokens(outsider).access_token
    other_headers = {
        **headers,
        "HTTP_AUTHORIZATION": f"Bearer {token}",
    }
    assert (
        client.put(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **other_headers,
        ).status_code
        == 404
    )
    assert client.get(f"{path}activity/", **other_headers).status_code == 404
    assert client.delete(path, **other_headers).status_code == 204
    assert MobileDevice.objects.filter(id=device_id, user=user).exists()
    assert client.delete(path, **headers).status_code == 204
    assert client.delete(path, **headers).status_code == 204


def test_configuration_only_admins_and_organization_credentials(client, setup, headers):
    org, user, _, _, _, credentials = setup
    member = User.objects.create_user(email="member@example.com")
    Membership.objects.create(user=member, organization=org)
    token = DjangoJWTBackend().generate_tokens(member).access_token
    member_headers = {
        **headers,
        "HTTP_AUTHORIZATION": f"Bearer {token}",
    }
    path = "/api/v1/notifications/configuration/"
    assert client.get(
        "/api/v1/notifications/capabilities/", **member_headers
    ).json() == {"ios": True, "android": True, "live_activities": True}
    assert client.get(path, **member_headers).status_code == 403
    assert (
        client.put(
            path, data="{}", content_type="application/json", **member_headers
        ).status_code
        == 403
    )
    response = client.put(
        path,
        data=json.dumps({"apns_credential_id": str(credentials["apns"].id)}),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 200
    assert "private_key" not in response.content.decode()
    credentials["apns"].organization = Organization.objects.create(
        name="Other", slug="other"
    )
    credentials["apns"].save()
    response = client.put(
        path,
        data=json.dumps({"apns_credential_id": str(credentials["apns"].id)}),
        content_type="application/json",
        **headers,
    )
    assert response.status_code == 400
    assert NotificationService().configuration(user, org.id)["ios"] is False


def test_invalid_native_registration_and_missing_provider(client, setup, headers):
    for payload in (
        {"platform": "web", "app_id": "com.opencuria.mobile"},
        {"platform": "ios", "app_id": "attacker.app"},
        {
            "platform": "ios",
            "app_id": "com.opencuria.mobile",
            "push_token": "token\nsecret",
        },
    ):
        assert (
            client.put(
                f"/api/v1/notifications/devices/{uuid.uuid4()}/",
                data=json.dumps(payload),
                content_type="application/json",
                **headers,
            ).status_code
            == 422
        )
    org, user, *_ = setup
    PushConfiguration.objects.filter(organization=org).delete()
    with pytest.raises(ValueError, match="not configured"):
        NotificationService().register(
            user,
            org.id,
            uuid.uuid4(),
            DeviceRegistrationIn(
                platform="android",
                app_id="com.opencuria.mobile",
                notifications_enabled=True,
                push_token="fcm-token",
            ),
        )


@pytest.mark.parametrize("engine", ["native", "claude", "codex"])
@pytest.mark.django_db(transaction=True)
def test_finished_signal_is_idempotent_and_no_transcript_leaks(setup, device, engine):
    _, _, _, session, message, _ = setup
    message.harness_id = engine
    message.content = "This private response must not appear on a lock screen"
    message.save()
    finish(message)
    asyncio.run(publish_assistant_run_finished(message.id, session_id=str(session.id)))
    assert PushDelivery.objects.filter(kind="alert").count() == 1
    NotificationService().record_finished(message.id)
    jobs = list(PushDelivery.objects.filter(kind="alert"))
    assert len(jobs) == 1
    assert jobs[0].payload["phase"] == "completed"
    assert message.content not in json.dumps(jobs[0].payload)


@pytest.mark.parametrize(
    "reason,error,phase,count",
    [
        ("error", "provider failed", "error", 1),
        ("aborted", "aborted by user", "interrupted", 0),
    ],
)
def test_outcomes_distinguish_failure_and_user_stop(
    setup, device, reason, error, phase, count
):
    message = setup[4]
    finish(message, reason, error)
    NotificationService().record_finished(message.id)
    assert PushDelivery.objects.filter(kind="alert").count() == count
    if count:
        assert PushDelivery.objects.get(kind="alert").payload["phase"] == phase


def test_child_completion_and_foreign_workspace_do_not_notify(setup, device):
    org, _, workspace, session, _, _ = setup
    child = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, parent=session
    )
    child_message = HarnessMessage.objects.create(
        session=child, role="assistant", completed_at=timezone.now()
    )
    NotificationService().record_finished(child_message.id)
    assert not PushDelivery.objects.exists()
    owner = User.objects.create_user(email="other-owner@example.com")
    workspace.created_by = owner
    workspace.save()
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    assert not PushDelivery.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_gate_events_include_child_attention_and_resolutions(setup, device):
    org, _, workspace, session, _, _ = setup
    child = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, parent=session, status="busy"
    )
    question = QuestionRequest.objects.create(
        organization_id=org.id, workspace_id=workspace.id, session_id=child.id
    )
    harness = HarnessService(emit=AsyncMock())
    data = {
        "session_id": str(child.id),
        "root_session_id": str(session.id),
        "request_id": str(question.id),
    }
    asyncio.run(
        harness._emit_frontend("harness.question_required", data, str(workspace.id))
    )
    alert = PushDelivery.objects.select_related("device__user").get(kind="alert")
    assert alert.payload["session_id"] == str(session.id)
    rows = ConversationActivityService().active_rows(
        device.organization_id, device.user_id
    )
    assert rows[0][1] == "waiting_for_input"
    question.status = "answered"
    question.save()
    service = NotificationService(providers=providers())
    result = asyncio.run(service.deliver(alert))
    assert result.code == "attention_resolved"
    assert not service.providers.send_apns.called


def test_permission_phase_is_current_and_payload_is_bounded(setup, device):
    org, user, workspace, session, _, _ = setup
    session.title = "😀" * 255
    session.save()
    workspace.name = "😀" * 255
    workspace.save()
    PermissionRequest.objects.create(
        organization_id=org.id,
        workspace_id=workspace.id,
        session_id=session.id,
        tool="exec",
    )
    snapshot = NotificationService().snapshot(user, org.id, device.id)
    assert snapshot["activeCount"] == 1
    assert snapshot["activities"][0]["phase"] == "waiting_for_approval"
    assert len(json.dumps(snapshot, ensure_ascii=False).encode()) < 4096


@pytest.mark.parametrize(
    "model,phase",
    [
        (PermissionRequest, "waiting_for_approval"),
        (QuestionRequest, "waiting_for_input"),
    ],
)
def test_attention_targets_its_own_root_among_runs_in_one_workspace(
    setup, device, model, phase
):
    org, user, workspace, session, _, _ = setup
    other = HarnessSession.objects.create(
        workspace=workspace, organization_id=org.id, status="busy", title="Other run"
    )
    HarnessMessage.objects.create(session=other, role="assistant")
    child = session
    for _ in range(2):
        child = HarnessSession.objects.create(
            workspace=workspace, organization_id=org.id, parent=child, status="busy"
        )
    model.objects.create(
        organization_id=org.id,
        workspace_id=workspace.id,
        session_id=child.id,
        **({"tool": "exec"} if model is PermissionRequest else {}),
    )
    snapshot = NotificationService().snapshot(user, org.id, device.id)
    assert snapshot["activeCount"] == 2
    assert snapshot["activities"][0]["sessionId"] == str(session.id)
    assert {row["sessionId"]: row["phase"] for row in snapshot["activities"]} == {
        str(session.id): phase,
        str(other.id): "running",
    }


def test_workspace_organization_change_fences_old_notification_scope(setup, device):
    _, _, workspace, session, message, _ = setup
    other = Organization.objects.create(name="Moved workspace", slug="moved-workspace")
    runner = workspace.runner
    runner.organization = other
    runner.save()
    finish(message)
    NotificationService().record_finished(message.id)
    assert ConversationActivityService().source(session.id) is None
    assert not ConversationActivityService().active_rows(
        device.organization_id, device.user_id
    )
    assert not PushDelivery.objects.exists()


def test_token_rotation_and_account_switch_retire_previous_binding(setup, device):
    org, user, *_ = setup
    service = NotificationService()
    registration = DeviceRegistrationIn(
        platform="ios",
        app_id="com.opencuria.mobile.dev",
        push_token="cd" * 32,
        notifications_enabled=True,
    )
    service.register(user, org.id, device.id, registration)
    device.refresh_from_db()
    assert decrypt_value(device.encrypted_push_token) == "cd" * 32
    other = User.objects.create_user(email="new-account@example.com")
    Membership.objects.create(user=other, organization=org)
    next_device = service.register(other, org.id, uuid.uuid4(), registration)
    assert MobileDevice.objects.filter(id=next_device.id).exists()
    assert not MobileDevice.objects.filter(id=device.id).exists()


def test_outbox_claim_recovery_and_fencing(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    first = NotificationRepository.claim()
    assert first is not None
    assert NotificationRepository.claim() is None
    PushDelivery.objects.filter(id=first.id).update(
        locked_until=timezone.now() - timedelta(seconds=1)
    )
    second = NotificationRepository.claim()
    assert second.claim_token != first.claim_token
    NotificationRepository.settle(first, "sent")
    assert PushDelivery.objects.get(id=first.id).status == "pending"
    NotificationRepository.settle(second, "sent")
    assert PushDelivery.objects.get(id=first.id).status == "sent"


@pytest.mark.django_db(transaction=True)
def test_transient_delivery_retries_then_acknowledges(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    transport = providers(DeliveryResult("retry", "apns_503"))
    service = NotificationService(providers=transport)
    assert asyncio.run(service.process_one())
    job = PushDelivery.objects.get(kind="alert")
    assert job.status == "pending" and job.attempts == 1
    assert job.available_at > timezone.now()
    PushDelivery.objects.filter(id=job.id).update(available_at=timezone.now())
    transport.send_apns.return_value = DeliveryResult("sent")
    assert asyncio.run(service.process_one())
    job.refresh_from_db()
    assert job.status == "sent" and job.attempts == 2
    assert transport.send_apns.call_args.kwargs["payload"]["body"]["sessionId"] == str(
        setup[3].id
    )


@pytest.mark.django_db(transaction=True)
def test_activity_token_registration_updates_and_ends_in_background(setup, device):
    org, user, _, session, message, _ = setup
    transport = providers()
    service = NotificationService(providers=transport)
    service.register_activity(
        user,
        org.id,
        device.id,
        ActivityRegistrationIn(activity_id="activity-1", push_token="ef" * 32),
    )
    assert asyncio.run(service.process_one())
    aps = transport.send_apns.call_args.kwargs["payload"]["aps"]
    assert aps["event"] == "update"
    assert json.loads(aps["content-state"]["props"])["activeCount"] == 1
    finish(message)
    session.status = "idle"
    session.save()
    service.record_finished(message.id)
    while asyncio.run(service.process_one()):
        pass
    activity_calls = [
        call
        for call in transport.send_apns.call_args_list
        if call.kwargs.get("live_activity")
    ]
    assert activity_calls[-1].kwargs["payload"]["aps"]["event"] == "end"
    ended = activity_calls[-1].kwargs["payload"]["aps"]
    assert ended["dismissal-date"] == ended["timestamp"]
    device.refresh_from_db()
    assert device.encrypted_activity_token == ""


@pytest.mark.django_db(transaction=True)
def test_invalid_provider_token_disables_only_rejected_destination(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    service = NotificationService(
        providers=providers(DeliveryResult("invalid", "apns_unregistered"))
    )
    asyncio.run(service.process_one())
    device.refresh_from_db()
    assert not device.notifications_enabled
    assert not device.encrypted_push_token


@pytest.mark.django_db(transaction=True)
def test_membership_revocation_and_expired_jobs_never_reach_provider(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    service = NotificationService(providers=providers())
    Membership.objects.filter(user=setup[1]).delete()
    asyncio.run(service.process_one())
    assert not service.providers.send_apns.called
    assert PushDelivery.objects.get(kind="alert").error_code == "registration_expired"
    PushDelivery.objects.all().update(
        status="pending", created_at=timezone.now() - timedelta(minutes=16)
    )
    asyncio.run(service.process_one())
    assert PushDelivery.objects.get(kind="alert").status == "expired"


@pytest.mark.django_db(transaction=True)
def test_android_alert_keeps_direct_fcm_navigation_data(setup):
    org, user, _, session, message, _ = setup
    NotificationService().register(
        user,
        org.id,
        uuid.uuid4(),
        DeviceRegistrationIn(
            platform="android",
            app_id="com.opencuria.mobile.dev",
            push_token="fcm-device",
            notifications_enabled=True,
        ),
    )
    finish(message)
    service = NotificationService(providers=providers())
    service.record_finished(message.id)
    asyncio.run(service.process_one())
    data = service.providers.send_fcm.call_args.kwargs["data"]
    assert data["sessionId"] == str(session.id)
    assert data["organizationId"] == str(org.id)
    assert not service.providers.send_apns.called


def test_new_opt_in_does_not_replay_earlier_completions(setup, device):
    org, user, _, _, message, _ = setup
    finish(message)
    service = NotificationService()
    off = DeviceRegistrationIn(platform="ios", app_id="com.opencuria.mobile.dev")
    service.register(user, org.id, device.id, off)
    on = off.model_copy(update={"notifications_enabled": True, "push_token": "ab" * 32})
    service.register(user, org.id, device.id, on)
    service.record_finished(message.id)
    assert not PushDelivery.objects.exists()


def test_repair_of_missed_attention_is_repeat_safe(setup, device):
    org, _, workspace, session, _, _ = setup
    PermissionRequest.objects.create(
        organization_id=org.id,
        workspace_id=workspace.id,
        session_id=session.id,
        tool="exec",
    )
    for _ in range(2):
        for event, data in ConversationActivityService().recent_attention_events():
            NotificationService().record_activity(event, data)
    assert PushDelivery.objects.filter(kind="alert").count() == 1
    assert (
        PushDelivery.objects.get(kind="alert").payload["phase"]
        == "waiting_for_approval"
    )


def test_configuration_rejects_invalid_key_without_disclosing_secret(setup):
    org, user, _, _, _, credentials = setup
    credential = credentials["apns"]
    from common.utils import encrypt_value

    credential.encrypted_value = encrypt_value('{"private_key":"private-sentinel"}')
    credential.save()
    with pytest.raises(ValueError, match="Invalid APNS credential") as error:
        NotificationService().configure(
            user, org.id, PushConfigurationIn(apns_credential_id=credential.id)
        )
    assert "private-sentinel" not in str(error.value)
