"""Runner entry point — WebSocket daemon that connects to the backend.

Usage:
    python -m src serve          # connect to backend via WebSocket
    python -m src serve --api-token <token>  # with explicit token
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Annotated

import structlog
import typer

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
    settings = RunnerSettings()
    if backend_url:
        settings.backend_url = backend_url
    if api_token:
        settings.api_token = api_token

    _configure_logging(settings)
    log = structlog.get_logger("runner")

    if not settings.api_token:
        log.error("api_token_required", hint="Set RUNNER_API_TOKEN or use --api-token")
        sys.exit(1)

    async def _run() -> None:
        from .config import RUNTIME_DOCKER, RUNTIME_QEMU
        from .runtime.docker_runtime import DockerRuntime
        from .service import WorkspaceService
        from .interfaces.websocket import WebSocketInterface

        # Build runtime backends based on configuration
        runtimes: dict[str, object] = {}
        enabled = settings.enabled_runtime_list

        if RUNTIME_DOCKER in enabled:
            runtimes[RUNTIME_DOCKER] = DockerRuntime(
                base_url=settings.docker_socket,
            )
            log.info("runtime_enabled", runtime=RUNTIME_DOCKER)

        if RUNTIME_QEMU in enabled:
            from .runtime.qemu_runtime import QemuRuntime

            runtimes[RUNTIME_QEMU] = QemuRuntime(settings=settings)
            log.info("runtime_enabled", runtime=RUNTIME_QEMU)

        if not runtimes:
            log.error(
                "no_runtimes_enabled",
                hint="Set RUNNER_ENABLED_RUNTIMES to 'docker', 'qemu', or 'docker,qemu'",
            )
            sys.exit(1)

        service = WorkspaceService(runtimes, settings)
        await service.sync_from_runtime()
        ws_interface = WebSocketInterface(service, settings)

        log.info(
            "runner_starting",
            backend_url=settings.backend_url,
            runtimes=list(runtimes.keys()),
        )

        try:
            await ws_interface.start()
        except asyncio.CancelledError:
            log.info("runner_interrupted")
        except KeyboardInterrupt:
            log.info("runner_interrupted")
        except Exception:
            log.exception("runner_fatal_error")
        finally:
            await ws_interface.stop()
            log.info("runner_stopped")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        pass


def main() -> None:
    """Entry point for ``python -m src.main``."""
    settings = RunnerSettings()
    _configure_logging(settings)
    app()


if __name__ == "__main__":
    main()
