"""Only explicit execution-process composition may own live agent tasks."""

from __future__ import annotations

import pytest

from apps.harness import harness_service as application
from apps.harness.command_service import RemoteHarnessService
from apps.harness.harness_service import HarnessService


@pytest.fixture
def isolated_application(monkeypatch):
    monkeypatch.setattr(application, "_default_harness_service", None)


def test_api_default_is_command_submission_without_live_task_ownership(
    isolated_application,
) -> None:
    service = application.get_harness_service()
    assert isinstance(service, RemoteHarnessService)
    assert service is application.get_harness_service()
    assert not service._reads.run_registry.records
    with pytest.raises(RuntimeError, match="already configured"):
        application.configure_execution_service(HarnessService())


def test_execution_owner_must_be_installed_before_any_api_facade(
    isolated_application,
) -> None:
    service = HarnessService()
    application.configure_execution_service(service)
    assert application.get_harness_service() is service
    application.configure_execution_service(service)
    with pytest.raises(RuntimeError, match="already configured"):
        application.configure_execution_service(HarnessService())
