"""Durable worker admission, ownership and restart-safety tests."""

from __future__ import annotations

import uuid

import pytest
from django.utils import timezone

from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.models import (
    HarnessMessage,
    HarnessRun,
    HarnessRunCommand,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
    HarnessWorkerRegistration,
)
from apps.harness.worker_protocol import (
    HarnessDeployment,
    HarnessWorkerRepository,
)
from common.exceptions import ConflictError


def _deployment(monkeypatch, **values) -> HarnessDeployment:
    """Construct a valid isolated deployment environment."""
    defaults = {
        "OPENCURIA_HARNESS_MODE": "worker",
        "OPENCURIA_DEPLOYMENT_VERSION": "release-2026-10",
        "OPENCURIA_WORKER_ID": "worker-a-1",
    }
    defaults.update(values)
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    return HarnessDeployment.from_environment()


@pytest.mark.parametrize("mode", ["local", "gateway", "worker"])
def test_deployment_environment_validation(monkeypatch, mode):
    if mode != "local":
        monkeypatch.setattr(
            "django.conf.settings.DEPLOYMENT_RELEASE",
            {"version": "release-2026-10"},
            raising=False,
        )
    _deployment(monkeypatch, OPENCURIA_HARNESS_MODE=mode)
    if mode == "local":
        monkeypatch.delenv("OPENCURIA_DEPLOYMENT_VERSION", raising=False)
        monkeypatch.delenv("OPENCURIA_WORKER_ID", raising=False)
        assert HarnessDeployment.from_environment().version == "local"
    elif mode == "gateway":
        monkeypatch.delenv("OPENCURIA_WORKER_ID", raising=False)
        assert HarnessDeployment.from_environment().worker_id == ""
        assert HarnessDeployment.from_environment().gateway_role == "gateway"
    else:
        assert HarnessDeployment.from_environment().worker_id == "worker-a-1"


@pytest.mark.parametrize(
    "values",
    [
        {"OPENCURIA_HARNESS_MODE": "wat"},
        {"OPENCURIA_HARNESS_MODE": "worker", "OPENCURIA_WORKER_ID": ""},
        {"OPENCURIA_HARNESS_MODE": "gateway", "OPENCURIA_DEPLOYMENT_VERSION": ""},
    ],
)
def test_bad_deployment_settings_fail_closed(monkeypatch, values):
    with pytest.raises(ValueError):
        _deployment(monkeypatch, **values)


def test_release_version_requires_matching_manifest(monkeypatch):
    """A managed deployment never starts with absent or mismatched manifest metadata."""
    monkeypatch.setenv("OPENCURIA_HARNESS_MODE", "worker")
    monkeypatch.setenv("OPENCURIA_DEPLOYMENT_VERSION", "release-v1")
    monkeypatch.setenv("OPENCURIA_WORKER_ID", "manifest-worker")
    monkeypatch.setattr(
        "django.conf.settings.DEPLOYMENT_RELEASE", {"version": ""}, raising=False
    )
    with pytest.raises(ValueError, match="container release manifest"):
        HarnessDeployment.from_environment()
    monkeypatch.setattr(
        "django.conf.settings.DEPLOYMENT_RELEASE",
        {"version": "release-v2"},
        raising=False,
    )
    with pytest.raises(ValueError, match="container release manifest"):
        HarnessDeployment.from_environment()


@pytest.mark.django_db(transaction=True)
def test_worker_registration_is_unique_and_drain_is_sticky():
    worker = HarnessWorkerRepository.register("worker-unique", "release-v1", pid=321)
    assert worker.state == HarnessWorkerRegistration.State.READY
    assert HarnessWorkerRepository.ready_for_version("release-v1")
    assert HarnessWorkerRepository.set_draining("worker-unique")
    assert not HarnessWorkerRepository.set_draining("worker-unique")
    assert HarnessWorkerRepository.heartbeat("worker-unique")
    worker.refresh_from_db()
    assert worker.state == HarnessWorkerRegistration.State.DRAINING
    assert not HarnessWorkerRepository.ready_for_version("release-v1")
    with pytest.raises(ConflictError, match="already used"):
        HarnessWorkerRepository.register("worker-unique", "release-v1", pid=322)


@pytest.mark.django_db(transaction=True)
def test_native_engine_uses_same_durable_attempt_fence(harness_workspace):
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="native",
        status=HarnessSessionStatus.BUSY,
    )
    user = HarnessMessage.objects.create(
        session=session, role="user", content="stored prompt only", position=0
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", harness_id="native", position=1
    )
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=assistant,
        organization_id=session.organization_id,
        harness_id="native",
        owner_worker_id="worker-native",
        deployment_version="release-v2",
        status=HarnessRunStatus.RUNNING,
    )
    assert run.assistant_message_id == assistant.id
    assert user.content == "stored prompt only"
    assert HarnessRunRepository.get_open_for_session(session.id).id == run.id
    assert not HarnessRunCommand.objects.filter(
        run=run, command_type=HarnessRunCommand.Type.START
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_outbox_claim_start_and_worker_generation_pinning(harness_workspace):
    worker = HarnessWorkerRepository.register("worker-claim", "release-v3", pid=1)
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="native",
    )
    user = HarnessMessage.objects.create(
        session=session, role="user", content="prompt", position=0
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", harness_id="native", position=1
    )
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=assistant,
        organization_id=session.organization_id,
        harness_id="native",
        deployment_version="release-v3",
    )
    command = HarnessRunCommand.objects.create(
        run=run,
        command_type=HarnessRunCommand.Type.START,
        deployment_version="release-v3",
        target_worker_id=worker.worker_id,
    )
    assert (
        HarnessWorkerRepository.claim_next("worker-claim", "release-v3").id
        == command.id
    )
    assert HarnessWorkerRepository.claim_next("worker-claim", "release-v3") is None
    started = HarnessWorkerRepository.start_claimed(command.id, worker.worker_id)
    assert started is not None
    run.refresh_from_db()
    assert run.status == HarnessRunStatus.RUNNING
    assert run.owner_worker_id == worker.worker_id
    assert HarnessWorkerRepository.finish_command(command.id, worker.worker_id)
    command.refresh_from_db()
    assert command.state == HarnessRunCommand.State.DONE
    assert user.content == "prompt"


@pytest.mark.django_db(transaction=True)
def test_gateway_admission_atomically_rolls_back_without_ready_worker(
    harness_workspace,
):
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="native",
    )
    with pytest.raises(ConflictError, match="No ready harness worker"):
        HarnessWorkerRepository.admit_run(
            session_id=session.id,
            prompt="not persisted",
            model="provider/model",
            reasoning_effort="",
            provider="provider",
            skill_ids=[],
            engine_id="native",
            initiated_by_id=None,
            deployment_version="release-no-worker",
            gateway=True,
        )
    session.refresh_from_db()
    assert session.status == HarnessSessionStatus.IDLE
    assert not HarnessMessage.objects.filter(session=session).exists()
    assert not HarnessRun.objects.filter(session=session).exists()
    assert not HarnessRunCommand.objects.filter(run__session=session).exists()


@pytest.mark.django_db(transaction=True)
def test_abort_intent_is_durable_and_never_reassigns_run(harness_workspace):
    worker = HarnessWorkerRepository.register("worker-abort", "release-v4", pid=2)
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="native",
        status=HarnessSessionStatus.BUSY,
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", harness_id="native", position=0
    )
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=assistant,
        organization_id=session.organization_id,
        harness_id="native",
        owner_worker_id=worker.worker_id,
        deployment_version=worker.deployment_version,
        status=HarnessRunStatus.RUNNING,
    )
    assert HarnessWorkerRepository.request_abort([session.id]) == 1
    assert HarnessWorkerRepository.request_abort([session.id]) == 1
    run.refresh_from_db()
    assert run.abort_requested_at is not None
    assert (
        HarnessRunCommand.objects.filter(
            run=run,
            command_type=HarnessRunCommand.Type.ABORT,
            target_worker_id=worker.worker_id,
        ).count()
        == 1
    )
    assert run.status == HarnessRunStatus.RUNNING
    commands = HarnessWorkerRepository.claim_control_commands(worker.worker_id)
    assert len(commands) == 1
    assert HarnessWorkerRepository.finish_command(commands[0].id, worker.worker_id)


@pytest.mark.django_db(transaction=True)
def test_lost_worker_never_auto_reclaims_or_replays_unknown_run(harness_workspace):
    worker = HarnessWorkerRepository.register("worker-lost", "release-v5", pid=3)
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="native",
        status=HarnessSessionStatus.BUSY,
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", harness_id="native", position=0
    )
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=assistant,
        organization_id=session.organization_id,
        harness_id="native",
        owner_worker_id=worker.worker_id,
        deployment_version=worker.deployment_version,
        status=HarnessRunStatus.RUNNING,
    )
    HarnessWorkerRepository.mark_stopped(worker.worker_id)
    with pytest.raises(ValueError, match="confirmed_dead"):
        HarnessWorkerRepository.recover_dead_worker(
            worker.worker_id, confirmed_dead=False
        )
    assert (
        HarnessWorkerRepository.recover_dead_worker(
            worker.worker_id, confirmed_dead=True
        )
        == 1
    )
    assert HarnessWorkerRepository.claim_next("another-worker", "release-v5") is None
    run.refresh_from_db()
    assert run.owner_worker_id == worker.worker_id
    assert run.status == HarnessRunStatus.RUNNING
    assert assistant.completed_at is None
    assert timezone.now() >= run.created_at


@pytest.mark.django_db(transaction=True)
def test_drain_keeps_child_admission_pinned_to_root_owner(harness_workspace):
    worker = HarnessWorkerRepository.register("worker-drain", "release-drain", pid=44)
    root_session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        status=HarnessSessionStatus.BUSY,
    )
    root_message = HarnessMessage.objects.create(
        session=root_session, role="assistant", harness_id="native", position=0
    )
    root = HarnessRun.objects.create(
        session=root_session,
        assistant_message=root_message,
        organization_id=root_session.organization_id,
        harness_id="native",
        owner_worker_id=worker.worker_id,
        deployment_version=worker.deployment_version,
        status=HarnessRunStatus.RUNNING,
    )
    child_session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        parent=root_session,
        status=HarnessSessionStatus.IDLE,
    )
    child_user, child_assistant, child_run, _ = HarnessWorkerRepository.admit_run(
        session_id=child_session.id,
        prompt="lazy child admission after drain",
        model="provider/model",
        reasoning_effort="",
        provider="provider",
        skill_ids=[],
        engine_id="native",
        initiated_by_id=None,
        deployment_version="release-drain",
        gateway=False,
        owner_worker_id=worker.worker_id,
        inherited_worker_id=worker.worker_id,
        inherited_version=worker.deployment_version,
        root_run_id=root.id,
    )
    assert child_user.content == "lazy child admission after drain"
    assert child_run.owner_worker_id == worker.worker_id
    assert child_run.deployment_version == root.deployment_version
    assert child_run.root_run_id == root.id
    assert not HarnessRunCommand.objects.filter(run=child_run).exists()
    assert HarnessWorkerRepository.set_draining(worker.worker_id)
    assert HarnessWorkerRepository.claim_next(worker.worker_id, "release-drain") is None
    assert child_assistant.id == child_run.assistant_message_id


def test_runtime_authority_is_context_local():
    from apps.harness.runtime_authority import (
        RunAuthority,
        current_run_authority,
        use_run_authority,
    )

    authority = RunAuthority(uuid.uuid4(), uuid.uuid4(), "w", "v", uuid.uuid4())
    assert current_run_authority() is None
    with use_run_authority(authority):
        assert current_run_authority() == authority
    assert current_run_authority() is None


@pytest.mark.django_db(transaction=True)
def test_pending_root_before_drain_is_claimed_once(harness_workspace):
    """Admission committed before drain may finish, but no new roots are taken."""
    HarnessWorkerRepository.register("pre-drain", "release-drain", pid=45)
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
    )
    _, _, run, _ = HarnessWorkerRepository.admit_run(
        session_id=session.id,
        prompt="committed",
        model="model",
        provider="provider",
        reasoning_effort="",
        skill_ids=[],
        engine_id="native",
        initiated_by_id=None,
        deployment_version="release-drain",
        gateway=True,
    )
    assert HarnessWorkerRepository.set_draining("pre-drain")
    assert not HarnessWorkerRepository.drained("pre-drain")
    command = HarnessWorkerRepository.pending_pre_drain_roots("pre-drain")[0]
    assert HarnessWorkerRepository.claim_pre_drain_root(command.id, "pre-drain")
    assert HarnessWorkerRepository.claim_pre_drain_root(command.id, "pre-drain") is None
    assert HarnessWorkerRepository.start_claimed(command.id, "pre-drain").id == run.id
    assert HarnessWorkerRepository.start_claimed(command.id, "pre-drain") is None


@pytest.mark.django_db(transaction=True)
def test_draining_release_blocks_restarted_worker_root_admission(harness_workspace):
    """An auto-restarted container must not reopen a globally draining release."""
    from apps.deployments.services import register_release
    from common.deployment_compatibility import ReleaseManifest

    register_release(
        ReleaseManifest("globally-draining", 1, 1, 1, 1, 1), state="draining"
    )
    HarnessWorkerRepository.register("new-process", "globally-draining", pid=46)
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
    )
    with pytest.raises(ConflictError, match="No ready"):
        HarnessWorkerRepository.admit_run(
            session_id=session.id,
            prompt="must not enter",
            model="model",
            provider="provider",
            reasoning_effort="",
            skill_ids=[],
            engine_id="native",
            initiated_by_id=None,
            deployment_version="globally-draining",
            gateway=True,
        )
    assert not HarnessRun.objects.filter(session=session).exists()
