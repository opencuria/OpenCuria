"""Operator command to register, drain, or retire immutable releases."""

from __future__ import annotations

from django.core.management import BaseCommand, CommandError
from django.db import connection

from apps.deployments.models import DatabaseSchemaState, Release
from apps.deployments.services import (
    live_generation_manifests,
    register_release,
    require_compatible_releases,
    retire_release,
)
from common.deployment_compatibility import DeploymentUpgradeError, ReleaseManifest


class Command(BaseCommand):
    """Maintain retained release inventory used to block unsafe rollouts."""

    help = "Maintain retained release compatibility metadata"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "action", choices=("register", "drain", "rollback", "retire", "check")
        )
        parser.add_argument("--manifest", help="Release manifest path (register only)")
        parser.add_argument("--release-version", help="Immutable release version")

    def handle(self, *args, **options) -> None:
        if connection.vendor != "postgresql":
            raise CommandError("record_release requires PostgreSQL")
        action = options["action"]
        try:
            if action == "register":
                if not options["manifest"]:
                    raise DeploymentUpgradeError("--manifest is required for register")
                candidate = ReleaseManifest.from_file(options["manifest"])
                from django.conf import settings

                if candidate != ReleaseManifest.from_mapping(
                    settings.DEPLOYMENT_RELEASE
                ):
                    raise DeploymentUpgradeError(
                        "Container release metadata does not match its manifest"
                    )
                current_schema = (
                    DatabaseSchemaState.objects.filter(id=1)
                    .values_list("schema_version", flat=True)
                    .first()
                )
                require_compatible_releases(
                    (*live_generation_manifests(), candidate),
                    current_schema=current_schema,
                )
                register_release(candidate)
                self.stdout.write(f"Registered {candidate.version} (active)")
            elif action in {"drain", "rollback"}:
                self._set_state(
                    options["release_version"],
                    "draining" if action == "drain" else "rollback",
                )
            elif action == "retire":
                self._retire(options["release_version"])
            else:
                schema = (
                    DatabaseSchemaState.objects.filter(id=1)
                    .values_list("schema_version", flat=True)
                    .first()
                )
                require_compatible_releases(
                    live_generation_manifests(), current_schema=schema
                )
                self.stdout.write("All retained and live releases are compatible")
        except DeploymentUpgradeError as exc:
            raise CommandError(str(exc)) from exc

    def _set_state(self, version: str | None, state: str) -> None:
        if not version:
            raise CommandError("--release-version is required")
        if (
            state == "rollback"
            and Release.objects.filter(version=version, state="draining").exists()
        ):
            raise CommandError(
                "A draining release cannot become a rollback target after retirement "
                "has begun; register a new immutable release instead."
            )
        if (
            state == "draining"
            and Release.objects.filter(version=version, state="rollback").exists()
        ):
            raise CommandError(
                "Rollback retention is explicit; end that window before draining."
            )
        changed = (
            Release.objects.filter(version=version)
            .exclude(state="retired")
            .update(state=state, retired_at=None)
        )
        if not changed:
            raise CommandError(
                f"Release {version} is not registered or already retired"
            )
        if state == "draining":
            from apps.harness.models import HarnessWorkerRegistration
            from apps.harness.worker_protocol import HarnessWorkerRepository

            worker_ids = list(
                HarnessWorkerRegistration.objects.filter(
                    deployment_version=version,
                    state=HarnessWorkerRegistration.State.READY,
                ).values_list("worker_id", flat=True)
            )
            for worker_id in worker_ids:
                HarnessWorkerRepository.set_draining(worker_id)
        schema = (
            DatabaseSchemaState.objects.filter(id=1)
            .values_list("schema_version", flat=True)
            .first()
        )
        require_compatible_releases(live_generation_manifests(), current_schema=schema)
        self.stdout.write(f"Release {version} is now {state}")

    def _retire(self, version: str | None) -> None:
        if not version:
            raise CommandError("--release-version is required")
        from apps.harness.models import (
            HarnessRun,
            HarnessWorkerRegistration,
            QuestionRequest,
        )
        from apps.harness.permissions.models import PermissionRequest

        if Release.objects.filter(version=version, state="rollback").exists():
            raise CommandError(
                "This release is retained for rollback; end its rollback window first."
            )
        active_workers = HarnessWorkerRegistration.objects.filter(
            deployment_version=version,
            state__in=("ready", "draining"),
        ).exists()
        worker_ids = HarnessWorkerRegistration.objects.filter(
            deployment_version=version
        ).values_list("worker_id", flat=True)
        unresolved_owner_runs = HarnessRun.objects.filter(
            owner_worker_id__in=worker_ids,
            status__in=("starting", "running", "closing"),
        ).exists()
        active_runs = HarnessRun.objects.filter(
            deployment_version=version,
            status__in=("starting", "running", "closing"),
        ).exists()
        sessions = HarnessRun.objects.filter(deployment_version=version).values_list(
            "session_id", flat=True
        )
        pending_permission = PermissionRequest.objects.filter(
            status="pending", session_id__in=sessions
        ).exists()
        pending_question = QuestionRequest.objects.filter(
            status="pending", session_id__in=sessions
        ).exists()
        if (
            active_workers
            or unresolved_owner_runs
            or active_runs
            or pending_permission
            or pending_question
        ):
            raise CommandError(
                f"Cannot retire {version}: live/stopped worker record, owned run, "
                "or unresolved permission/question remains"
            )
        retire_release(version)
        self.stdout.write(f"Retired {version}; schema and side effects were untouched")
