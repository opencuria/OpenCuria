"""Native delivery: ownership, lifecycle, restart, encryption and proxy errors."""

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

from ..encryption import ACTIVITY_PADDED_SIZE
from ..models import MobileDevice, PushDelivery
from ..proxy import DeliveryResult
from ..repositories import NotificationRepository
from ..schemas import ActivityRegistrationIn, DeviceRegistrationIn
from ..services import NotificationService
from .conftest import registration

pytestmark = pytest.mark.django_db


def finish(message, reason="stop", error=""):
    message.finish = reason
    message.error = error
    message.completed_at = timezone.now()
    message.save(update_fields=["finish", "error", "completed_at"])


def proxy(result=DeliveryResult("sent")):
    class Proxy:
        send = AsyncMock(return_value=result)

    return Proxy()


def sent(transport, kind="alert"):
    """Messages handed to the proxy, newest last."""
    return [
        call.args[0]
        for call in transport.send.call_args_list
        if (call.args[0].type == "alert") == (kind == "alert")
    ]


@pytest.mark.parametrize("organization", [None, "", "not-a-uuid"])
def test_invalid_organization_context_is_rejected(client, headers, organization):
    headers.pop("HTTP_X_ORGANIZATION_ID")
    if organization is not None:
        headers["HTTP_X_ORGANIZATION_ID"] = organization
    response = client.get("/api/v1/notifications/capabilities/", **headers)
    assert response.status_code == 403
    assert response.json()["code"] == "authentication_error"


def test_device_api_enforces_scope_and_never_echoes_handles(
    client, setup, headers, device_keys
):
    org, user, *_ = setup
    device_id = uuid.uuid4()
    payload = {
        "platform": "ios",
        "push_handle": "sealed-proxy-handle",
        "encryption_key": device_keys.public,
        "auth_secret": device_keys.auth_secret,
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
    assert decrypt_value(device.encrypted_push_handle) == payload["push_handle"]
    assert payload["push_handle"] not in device.encrypted_push_handle
    assert decrypt_value(device.encrypted_auth_secret) == device_keys.auth_secret
    assert device.encryption_key == device_keys.public
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


def test_invalid_native_registrations_are_rejected(client, setup, headers, device_keys):
    valid = {
        "platform": "ios",
        "push_handle": "handle",
        "encryption_key": device_keys.public,
        "auth_secret": device_keys.auth_secret,
        "notifications_enabled": True,
    }
    for change in (
        {"platform": "web"},
        {"push_handle": "handle\nsecret"},
        {"encryption_key": "BAAA"},
        {"encryption_key": "B" + "A" * 86},
        {"auth_secret": "c2hvcnQ"},
    ):
        response = client.put(
            f"/api/v1/notifications/devices/{uuid.uuid4()}/",
            data=json.dumps({**valid, **change}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 422, change
    org, user, *_ = setup
    service = NotificationService()
    for change, message in (
        ({"push_handle": ""}, "push handle is required"),
        ({"encryption_key": "", "auth_secret": ""}, "encryption key is required"),
        ({"platform": "android"}, "only available on iOS"),
    ):
        with pytest.raises(ValueError, match=message):
            service.register(
                user, org.id, uuid.uuid4(), registration(device_keys, **change)
            )


@pytest.mark.parametrize("engine", ["native", "claude", "codex"])
@pytest.mark.django_db(transaction=True)
def test_finished_signal_is_idempotent_and_no_transcript_leaks(setup, device, engine):
    _, _, _, session, message = setup
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
    org, _, workspace, session, _ = setup
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
    org, _, workspace, session, _ = setup
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
    service = NotificationService(proxy=proxy())
    result = asyncio.run(service.deliver(alert))
    assert result.code == "attention_resolved"
    assert not service.proxy.send.called


def test_permission_phase_is_current_and_payload_fits_its_padding(setup, device):
    org, user, workspace, session, _ = setup
    session.title = "😀" * 255
    session.save()
    workspace.name = "😀" * 255
    workspace.save()
    for index in range(3):
        HarnessSession.objects.create(
            workspace=workspace,
            organization_id=org.id,
            created_by=user,
            title="😀" * 255,
            status="busy",
        )
    PermissionRequest.objects.create(
        organization_id=org.id,
        workspace_id=workspace.id,
        session_id=session.id,
        tool="exec",
    )
    snapshot = NotificationService().snapshot(user, org.id, device.id)
    assert snapshot["activeCount"] == 4
    assert snapshot["activities"][0]["phase"] == "waiting_for_approval"
    snapshot["activeCount"] = 99999
    for row in snapshot["activities"]:
        row["phase"] = "waiting_for_approval"
    size = len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= ACTIVITY_PADDED_SIZE


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
    org, user, workspace, session, _ = setup
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
    _, _, workspace, session, message = setup
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


def test_handle_rotation_and_account_switch_retire_previous_binding(
    setup, device, device_keys
):
    org, user, *_ = setup
    service = NotificationService()
    rotated = registration(device_keys, push_handle="rotated-handle")
    service.register(user, org.id, device.id, rotated)
    device.refresh_from_db()
    assert decrypt_value(device.encrypted_push_handle) == "rotated-handle"
    other = User.objects.create_user(email="new-account@example.com")
    Membership.objects.create(user=other, organization=org)
    next_device = service.register(other, org.id, uuid.uuid4(), rotated)
    assert MobileDevice.objects.filter(id=next_device.id).exists()
    assert not MobileDevice.objects.filter(id=device.id).exists()


def test_disabling_everything_forgets_handles_and_keys(setup, device, device_keys):
    org, user, *_ = setup
    NotificationService().register(
        user,
        org.id,
        device.id,
        registration(
            device_keys,
            push_handle="",
            encryption_key="",
            auth_secret="",
            notifications_enabled=False,
            live_activities_enabled=False,
        ),
    )
    device.refresh_from_db()
    assert not device.encrypted_push_handle
    assert not device.encryption_key
    assert not device.encrypted_auth_secret


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
def test_alerts_are_end_to_end_encrypted_for_the_device(setup, device, device_keys):
    _, _, _, session, message = setup
    message.content = "This private response must not appear on a lock screen"
    message.save()
    finish(message)
    NotificationService().record_finished(message.id)
    transport = proxy()
    asyncio.run(NotificationService(proxy=transport).process_one())
    (sent_alert,) = sent(transport)
    assert sent_alert.handle == "alert-handle"
    assert sent_alert.timestamp is None
    assert len(sent_alert.collapse_id) == 32
    for value in ("Build feature", "Push workspace", str(session.id), "finished"):
        assert value not in json.dumps(sent_alert.__dict__)
    plaintext = device_keys.open(sent_alert.payload)
    assert plaintext == {
        "title": "Agent finished",
        "body": "Build feature · Push workspace",
        "thread": str(session.id),
        "tag": sent_alert.collapse_id,
        "data": {
            "type": "opencuria.agent-activity",
            "deviceId": str(device.id),
            "organizationId": str(device.organization_id),
            "workspaceId": str(session.workspace_id),
            "sessionId": str(session.id),
            "phase": "completed",
        },
    }
    assert message.content not in json.dumps(plaintext)


@pytest.mark.django_db(transaction=True)
def test_transient_delivery_retries_then_acknowledges(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    transport = proxy(DeliveryResult("retry", "proxy_503_provider_unavailable"))
    service = NotificationService(proxy=transport)
    assert asyncio.run(service.process_one())
    job = PushDelivery.objects.get(kind="alert")
    assert job.status == "pending" and job.attempts == 1
    assert job.available_at > timezone.now()
    PushDelivery.objects.filter(id=job.id).update(available_at=timezone.now())
    transport.send.return_value = DeliveryResult("sent")
    assert asyncio.run(service.process_one())
    job.refresh_from_db()
    assert job.status == "sent" and job.attempts == 2


@pytest.mark.django_db(transaction=True)
def test_activity_handle_registration_updates_and_ends_in_background(
    setup, device, device_keys
):
    org, user, _, session, message = setup
    transport = proxy()
    service = NotificationService(proxy=transport)
    service.register_activity(
        user,
        org.id,
        device.id,
        ActivityRegistrationIn(activity_id="activity-1", push_handle="activity-handle"),
    )
    assert asyncio.run(service.process_one())
    update = sent(transport, "activity")[-1]
    assert update.type == "activity_update"
    assert update.handle == "activity-handle"
    assert update.stale_date == update.timestamp + 600
    assert device_keys.open(update.payload)["activeCount"] == 1
    finish(message)
    session.status = "idle"
    session.save()
    service.record_finished(message.id)
    while asyncio.run(service.process_one()):
        pass
    ended = sent(transport, "activity")[-1]
    assert ended.type == "activity_end"
    assert ended.dismissal_date == ended.timestamp
    assert device_keys.open(ended.payload)["activeCount"] == 0
    assert len(ended.payload) == len(update.payload)
    device.refresh_from_db()
    assert device.encrypted_activity_handle == ""


@pytest.mark.django_db(transaction=True)
def test_rejected_handle_disables_only_that_destination(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    service = NotificationService(
        proxy=proxy(DeliveryResult("invalid", "proxy_410_unregistered"))
    )
    asyncio.run(service.process_one())
    device.refresh_from_db()
    assert not device.notifications_enabled
    assert not device.encrypted_push_handle
    assert device.encryption_key


@pytest.mark.django_db(transaction=True)
def test_undecryptable_stored_keys_fail_without_retrying(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    MobileDevice.objects.filter(id=device.id).update(encryption_key="BAAA")
    transport = proxy()
    asyncio.run(NotificationService(proxy=transport).process_one())
    job = PushDelivery.objects.get(kind="alert")
    assert (job.status, job.error_code) == ("failed", "invalid_destination")
    assert not transport.send.called


@pytest.mark.django_db(transaction=True)
def test_membership_revocation_and_expired_jobs_never_reach_the_proxy(setup, device):
    finish(setup[4])
    NotificationService().record_finished(setup[4].id)
    service = NotificationService(proxy=proxy())
    Membership.objects.filter(user=setup[1]).delete()
    asyncio.run(service.process_one())
    assert not service.proxy.send.called
    assert PushDelivery.objects.get(kind="alert").error_code == "registration_expired"
    PushDelivery.objects.all().update(
        status="pending", created_at=timezone.now() - timedelta(minutes=16)
    )
    asyncio.run(service.process_one())
    assert PushDelivery.objects.get(kind="alert").status == "expired"


@pytest.mark.django_db(transaction=True)
def test_android_alert_uses_the_same_encrypted_contract(setup, device_keys):
    org, user, _, session, message = setup
    NotificationService().register(
        user,
        org.id,
        uuid.uuid4(),
        registration(
            device_keys,
            platform="android",
            push_handle="android-handle",
            live_activities_enabled=False,
        ),
    )
    finish(message)
    service = NotificationService(proxy=proxy())
    service.record_finished(message.id)
    asyncio.run(service.process_one())
    (alert,) = sent(service.proxy)
    assert alert.handle == "android-handle"
    data = device_keys.open(alert.payload)["data"]
    assert data["sessionId"] == str(session.id)
    assert data["organizationId"] == str(org.id)


def test_new_opt_in_does_not_replay_earlier_completions(setup, device, device_keys):
    org, user, _, _, message = setup
    finish(message)
    service = NotificationService()
    off = DeviceRegistrationIn(platform="ios")
    service.register(user, org.id, device.id, off)
    service.register(
        user,
        org.id,
        device.id,
        registration(device_keys, live_activities_enabled=False),
    )
    service.record_finished(message.id)
    assert not PushDelivery.objects.exists()


def test_repair_of_missed_attention_is_repeat_safe(setup, device):
    org, _, workspace, session, _ = setup
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
