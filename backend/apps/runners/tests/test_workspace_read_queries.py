"""Workspace discovery keeps metadata reads bounded as the list grows."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import Mock

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import APIKeyPermission
from apps.credentials.services import CredentialSvc
from apps.organizations.models import Membership
from apps.plugins.services import PluginService
from apps.runners.models import ImageInstance, Runner, Workspace
from apps.runners.schemas import WorkspaceOut
from apps.runners.services import RunnerService
from apps.runners.services.workspace_reads import WorkspaceReadService
from apps.runners.tests.test_image_versions_api import headers
from apps.runners.tests.workspace_read_cases import workspace_read_cases

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("count", [3, 30])
@pytest.mark.parametrize("with_metadata", [False, True])
def test_workspace_read_query_count_is_independent_of_list_size(
    runner: Runner,
    user: Any,
    django_assert_num_queries: Any,
    count: int,
    with_metadata: bool,
) -> None:
    """Count the real list, activity, relation reads and schema serialization."""
    workspaces = workspace_read_cases(runner, user, count, with_metadata=with_metadata)
    reads = WorkspaceReadService()
    with django_assert_num_queries(12 if with_metadata else 5):
        loaded = RunnerService().list_workspaces(
            organization_id=runner.organization_id, user=user
        )
        result = [
            WorkspaceOut(**data).model_dump() for data in reads.describe_many(loaded)
        ]
    assert [row["id"] for row in result] == [row.id for row in reversed(workspaces)]
    assert "opaque-secret" not in str(result)
    if with_metadata:
        for row in result:
            assert len(row["credential_ids"]) == len(row["plugin_ids"]) == 2
            assert row["base_image"]["update_available"] is (
                row["pending_base_image"] is not None
            )


@pytest.mark.parametrize("with_metadata", [False, True])
def test_workspace_list_adapter_uses_bulk_reads(
    runner: Runner, user: Any, with_metadata: bool
) -> None:
    """REST preserves ordering and metadata while avoiding per-row service calls."""
    Membership.objects.create(user=user, organization=runner.organization)
    auth = headers(user, runner.organization, [APIKeyPermission.WORKSPACES_READ])
    counts = []
    for count in (3, 30):
        workspace_read_cases(runner, user, count, with_metadata=with_metadata)
        with CaptureQueriesContext(connection) as captured:
            response = Client().get("/api/v1/workspaces/", **auth)
        assert response.status_code == 200
        counts.append(len(captured))
    assert len(response.json()) == 33
    assert counts[0] == counts[1]


def test_empty_workspace_metadata_requires_no_reads(
    runner: Runner, user: Any, django_assert_num_queries: Any
) -> None:
    """Empty lists do not load relations, image versions or attachment catalogs."""
    with django_assert_num_queries(1):
        workspaces = RunnerService().list_workspaces(
            organization_id=runner.organization_id, user=user
        )
        assert WorkspaceReadService().describe_many(workspaces) == []
    with django_assert_num_queries(0):
        assert CredentialSvc().workspace_credential_ids_many([]) == {}
        assert PluginService().list_workspace_plugin_ids_many([]) == {}


def test_bulk_selection_metadata_preserves_order_scope_and_secret_filter(
    runner: Runner, user: Any, django_assert_num_queries: Any
) -> None:
    """Only requested workspaces appear, with ordered IDs and no secret bodies."""
    included, excluded = workspace_read_cases(runner, user, 2, with_metadata=True)
    missing = uuid.uuid4()
    credentials, plugins = CredentialSvc(), PluginService()
    expected_credentials = [
        included.credentials.get(name=name).id for name in ("mcp_oauth", "env")
    ]
    expected_plugins = [
        included.plugin_activations.get(plugin__name=name).plugin_id
        for name in ("A last selected", "Z first selected")
    ]
    with django_assert_num_queries(2) as queries:
        actual_credentials = credentials.workspace_credential_ids_many(
            [included.id, missing]
        )
        actual_plugins = plugins.list_workspace_plugin_ids_many([included.id, missing])
    assert actual_credentials == {included.id: expected_credentials, missing: []}
    assert actual_plugins == {included.id: expected_plugins, missing: []}
    assert len(expected_credentials) == len(expected_plugins) == 2
    assert excluded.id not in actual_credentials
    assert all(
        "encrypted_value" not in query["sql"] for query in queries.captured_queries
    )


def test_workspace_metadata_read_failure_propagates(workspace: Workspace) -> None:
    """A failed batch must not silently present empty credential selections."""
    repository = Mock()
    credentials = Mock()
    credentials.workspace_credential_ids_many.side_effect = RuntimeError("read failed")
    reads = WorkspaceReadService(repository=repository, credentials=credentials)
    with pytest.raises(RuntimeError, match="read failed"):
        reads.describe_many([workspace])


def test_workspace_metadata_is_fresh_on_each_read(runner: Runner, user: Any) -> None:
    """Reusing the reader must see attachment edits and newly published versions."""
    [workspace] = workspace_read_cases(runner, user, 1, with_metadata=True)
    reads = WorkspaceReadService()
    first = reads.describe(workspace)
    base = workspace.base_image_instance
    latest = ImageInstance.objects.create(
        runner=runner,
        captured_image=base.captured_image,
        name="Just published",
        generation=3,
        runner_ref="new-publication",
    )
    workspace.credentials.clear()
    workspace.plugin_activations.all().delete()
    second = reads.describe(workspace)
    assert first["base_image"]["latest_version"] == 2
    assert second["base_image"]["latest_id"] == latest.id
    assert second["base_image"]["latest_version"] == 3
    assert second["credential_ids"] == second["plugin_ids"] == []
