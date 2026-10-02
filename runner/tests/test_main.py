"""CLI and lifecycle exit behavior for the runner entry point."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from src import main as runner_main
from src.interfaces.websocket_lifecycle import FatalConnectionError
from typer.testing import CliRunner

_RUNNER_SETTINGS = runner_main.RunnerSettings


@pytest.fixture
def cli() -> CliRunner:
    return CliRunner()


def _settings(**overrides: object) -> runner_main.RunnerSettings:
    """Return valid settings without consulting a developer's local .env."""
    values: dict[str, object] = {
        "_env_file": None,
        "api_token": "test-token",
        "enabled_runtimes": "docker",
    }
    values.update(overrides)
    return _RUNNER_SETTINGS(**values)


def _mock_runner_dependencies(
    monkeypatch: pytest.MonkeyPatch, interface, service
) -> None:
    """Replace runtime and service construction with local fakes."""
    monkeypatch.setattr(runner_main, "RunnerSettings", lambda **kwargs: _settings())
    monkeypatch.setattr(runner_main, "_configure_logging", lambda settings: None)
    monkeypatch.setattr("src.runtime.docker_runtime.DockerRuntime", MagicMock())
    monkeypatch.setattr("src.service.WorkspaceService", MagicMock(return_value=service))
    monkeypatch.setattr(
        "src.interfaces.websocket.WebSocketInterface",
        MagicMock(return_value=interface),
    )


def test_missing_token_returns_nonzero(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_main,
        "RunnerSettings",
        lambda **kwargs: _settings(api_token=""),
    )

    result = cli.invoke(runner_main.app, [])

    assert result.exit_code == 1
    assert "api_token_required" in result.output
    assert "test-token" not in result.output


@pytest.mark.parametrize(
    "option,value",
    [
        ("--backend-url", "https://user:password@example.test"),
        ("--backend-url", "https://example.test/path?token=secret"),
        ("--backend-url", "not-a-url"),
    ],
)
def test_invalid_cli_url_returns_nonzero_without_echoing_secrets(
    cli: CliRunner, option: str, value: str
) -> None:
    result = cli.invoke(runner_main.app, [option, value, "--api-token", "secret"])

    assert result.exit_code == 1
    assert "runner_configuration_invalid" in result.output
    assert "secret" not in result.output
    assert "password" not in result.output


def test_invalid_settings_return_nonzero(cli: CliRunner) -> None:
    result = cli.invoke(
        runner_main.app,
        ["--api-token", "secret"],
        env={"RUNNER_CONNECTION_TIMEOUT": "0"},
    )

    assert result.exit_code == 1
    assert "runner_configuration_invalid" in result.output
    assert "secret" not in result.output


def test_fatal_connection_error_returns_nonzero_and_stops(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    interface = MagicMock()
    interface.start = AsyncMock(
        side_effect=FatalConnectionError("authentication_rejected")
    )
    interface.stop = AsyncMock()
    service = MagicMock()
    service.sync_from_runtime = AsyncMock()
    _mock_runner_dependencies(monkeypatch, interface, service)

    result = cli.invoke(runner_main.app, [])

    assert result.exit_code == 1
    assert "authentication_rejected" in result.output
    interface.stop.assert_awaited_once()


def test_startup_exception_returns_nonzero_and_stops_interface(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    interface = MagicMock()
    interface.start = AsyncMock()
    interface.stop = AsyncMock()
    service = MagicMock()
    service.sync_from_runtime = AsyncMock(
        side_effect=RuntimeError("sensitive startup detail")
    )
    _mock_runner_dependencies(monkeypatch, interface, service)

    result = cli.invoke(runner_main.app, [])

    assert result.exit_code == 1
    assert "runner_startup_failed" in result.output
    assert "sensitive startup detail" not in result.output
    interface.start.assert_not_awaited()
    interface.stop.assert_awaited_once()


def test_normal_cancellation_returns_zero_and_stops_interface(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    interface = MagicMock()
    interface.start = AsyncMock(side_effect=asyncio.CancelledError())
    interface.stop = AsyncMock()
    service = MagicMock()
    service.sync_from_runtime = AsyncMock()
    _mock_runner_dependencies(monkeypatch, interface, service)

    result = cli.invoke(runner_main.app, [])

    assert result.exit_code == 0
    assert "runner_interrupted" in result.output
    interface.stop.assert_awaited_once()


def test_signal_handler_cancels_runner_and_stops_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise_signal() -> None:
        interface = MagicMock()
        interface.stop = AsyncMock()
        service = MagicMock()
        service.sync_from_runtime = AsyncMock()
        _mock_runner_dependencies(monkeypatch, interface, service)

        loop = asyncio.get_running_loop()
        signal_handlers = {}
        add_signal_handler = loop.add_signal_handler

        def capture_handler(sig, callback, *args) -> None:
            signal_handlers[sig] = callback

        monkeypatch.setattr(loop, "add_signal_handler", capture_handler)

        async def wait_for_shutdown() -> None:
            loop.call_soon(signal_handlers[runner_main.signal.SIGTERM])
            await asyncio.Event().wait()

        interface.start = wait_for_shutdown
        result = await runner_main._run_runner(_settings())

        assert result == 0
        interface.stop.assert_awaited_once()
        assert set(signal_handlers) == {
            runner_main.signal.SIGINT,
            runner_main.signal.SIGTERM,
        }
        # Verify cleanup can restore the loop's actual handlers after our capture.
        monkeypatch.setattr(loop, "add_signal_handler", add_signal_handler)

    asyncio.run(exercise_signal())


def test_startup_sync_failure_before_websocket_start_is_nonzero_and_stops(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    interface = MagicMock()
    interface.start = AsyncMock()
    interface.stop = AsyncMock()
    service = MagicMock()
    service.sync_from_runtime = AsyncMock(side_effect=OSError("backend detail"))
    _mock_runner_dependencies(monkeypatch, interface, service)

    result = cli.invoke(runner_main.app, [])

    assert result.exit_code == 1
    assert "backend detail" not in result.output
    interface.start.assert_not_awaited()
    interface.stop.assert_awaited_once()
