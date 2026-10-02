"""Runner entry point — WebSocket daemon that connects to the backend.

Usage:
    python -m src                # connect to backend via WebSocket
    python -m src --api-token <token>  # with explicit token
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Annotated

import structlog
import typer
from pydantic import ValidationError

from .config import RunnerSettings


def _configure_logging(settings: RunnerSettings) -> None:
    """Set up structlog with the configured format and level."""

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    if settings.log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _safe_fatal_code(error: Exception) -> str:
    """Return a safe structured failure code without logging arbitrary details."""
    code = error.args[0] if error.args else None
    if (
        isinstance(code, str)
        and 0 < len(code) <= 80
        and all(char.isascii() and (char.isalnum() or char in "_.:-") for char in code)
    ):
        return code
    return "fatal_connection_error"


async def _run_runner(settings: RunnerSettings) -> int:
    """Start the runner, stop its interface on every exit, and report status."""
    from .config import RUNTIME_DOCKER, RUNTIME_QEMU
    from .interfaces.websocket import WebSocketInterface
    from .interfaces.websocket_lifecycle import FatalConnectionError
    from .runtime.docker_runtime import DockerRuntime
    from .service import WorkspaceService

    log = structlog.get_logger("runner")
    ws_interface: WebSocketInterface | None = None
    exit_code = 0
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()

    def request_shutdown() -> None:
        if task is not None and not task.done():
            task.cancel()

    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            # Signal handlers are unavailable on some event-loop implementations.
            continue

    try:
        runtimes: dict[str, object] = {}
        enabled = settings.enabled_runtime_list

        if RUNTIME_DOCKER in enabled:
            runtimes[RUNTIME_DOCKER] = DockerRuntime(base_url=settings.docker_socket)
            log.info("runtime_enabled", runtime=RUNTIME_DOCKER)

        if RUNTIME_QEMU in enabled:
            from .runtime.qemu_runtime import QemuRuntime

            runtimes[RUNTIME_QEMU] = QemuRuntime(settings=settings)
            log.info("runtime_enabled", runtime=RUNTIME_QEMU)

        if not runtimes:
            log.error("no_runtimes_enabled")
            return 1

        service = WorkspaceService(runtimes, settings)
        ws_interface = WebSocketInterface(service, settings)
        await service.sync_from_runtime()

        # Do not log the configured URL: it may contain deployment-specific data.
        log.info("runner_starting", runtimes=list(runtimes))
        await ws_interface.start()
    except asyncio.CancelledError:
        log.info("runner_interrupted")
    except FatalConnectionError as exc:
        exit_code = 1
        log.error("runner_fatal_connection_error", code=_safe_fatal_code(exc))
    except Exception as exc:
        # Exception messages and validation errors can contain credentials/config.
        log.error("runner_startup_failed", error_type=type(exc).__name__)
        exit_code = 1
    finally:
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        if ws_interface is not None:
            try:
                await ws_interface.stop()
            except asyncio.CancelledError:
                log.error("runner_cleanup_cancelled")
                exit_code = 1
            except Exception as exc:
                log.error("runner_cleanup_failed", error_type=type(exc).__name__)
                exit_code = 1
        log.info("runner_stopped")

    return exit_code


# ---------------------------------------------------------------------------
# Main Typer app
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="opencuria-runner",
    help="opencuria Runner — connects to the backend and executes workspace commands.",
    no_args_is_help=True,
)


@app.command()
def serve(
    backend_url: Annotated[
        str | None,
        typer.Option("--backend-url", "-b", help="Override backend WebSocket URL"),
    ] = None,
    api_token: Annotated[
        str | None,
        typer.Option("--api-token", help="Override API token"),
    ] = None,
) -> None:
    """Start the runner in daemon mode, connecting to the backend via WebSocket."""
    overrides: dict[str, str] = {}
    if backend_url is not None:
        overrides["backend_url"] = backend_url
    if api_token is not None:
        overrides["api_token"] = api_token

    try:
        # Pydantic Settings merges these explicit CLI values with the environment
        # and validates all fields and model-level constraints in one pass.
        settings = RunnerSettings(**overrides)
    except (ValidationError, ValueError, TypeError) as exc:
        structlog.get_logger("runner").error(
            "runner_configuration_invalid", error_type=type(exc).__name__
        )
        raise typer.Exit(code=1) from None

    _configure_logging(settings)
    log = structlog.get_logger("runner")

    if not settings.api_token:
        log.error("api_token_required", hint="Set RUNNER_API_TOKEN or use --api-token")
        raise typer.Exit(code=1)

    try:
        exit_code = asyncio.run(_run_runner(settings))
    except KeyboardInterrupt:
        # asyncio.run may surface a keyboard interrupt from an unsupported loop.
        log.info("runner_interrupted")
        return
    except Exception as exc:
        # Keep unexpected top-level failures non-zero and avoid exception text.
        log.error("runner_startup_failed", error_type=type(exc).__name__)
        raise typer.Exit(code=1) from None

    if exit_code:
        raise typer.Exit(code=exit_code)


def main() -> None:
    """Entry point for ``python -m src.main``."""
    app()


if __name__ == "__main__":
    main()
