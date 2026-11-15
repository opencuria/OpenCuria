"""Workspace plugin runtime preparation for harness runs."""

# Imported pytest fixture names intentionally match injected arguments.
# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest

from apps.plugins.runtime import PluginCredentialConfigError
from apps.plugins.tests.test_runtime_queries import (
    _plugin,
    runtime_catalogue,  # noqa: F401
)
from apps.runners.services.workspace_plugin_runtime import (
    WorkspacePluginRuntimeService,
)


@pytest.mark.parametrize("count", [1, 15])
def test_run_preparation_queries_are_bounded(
    runtime_catalogue, count: int, django_assert_num_queries
) -> None:
    """Exercise run preparation including workspace reads and real decryption."""
    org, _, workspace, services = runtime_catalogue
    for index in range(count):
        _plugin(workspace, services, index)
    with django_assert_num_queries(11):
        prepared = WorkspacePluginRuntimeService().prepare(workspace.id, org.id)
    assert len(prepared.snapshot.plugins) == count
    assert all(
        row == {"api_key": "synthetic-token"} for row in prepared.plaintexts.values()
    )
    assert prepared.oauth_credentials == {}


def test_empty_selection_skips_credential_resolution(runtime_catalogue) -> None:
    """A workspace without effective plugins prepares an empty runtime."""
    org, _, workspace, _ = runtime_catalogue
    prepared = WorkspacePluginRuntimeService().prepare(workspace.id, org.id)
    assert prepared.snapshot.workspace_id == workspace.id
    assert prepared.snapshot.plugins == ()
    assert prepared.plaintexts == {}
    assert prepared.oauth_credentials == {}


def test_foreign_organization_fails_closed(runtime_catalogue) -> None:
    """A run in another organization never sees the workspace's plugins."""
    _, foreign, workspace, services = runtime_catalogue
    _plugin(workspace, services, 0)
    with pytest.raises(PluginCredentialConfigError, match="organization mismatch"):
        WorkspacePluginRuntimeService().prepare(workspace.id, foreign.id)


def test_missing_workspace_fails_closed(runtime_catalogue) -> None:
    """A deleted workspace fails before any plugin or credential is loaded."""
    org, *_ = runtime_catalogue
    with pytest.raises(PluginCredentialConfigError, match="organization mismatch"):
        WorkspacePluginRuntimeService().prepare(uuid.uuid4(), org.id)
