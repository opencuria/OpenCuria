"""Runner entry point — WebSocket daemon that connects to the backend.

Usage:
    python -m src                # connect to backend via WebSocket
    python -m src --api-token <token>  # with explicit token
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
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


async def build_workspace_service(settings: RunnerSettings, log=None):
    """Build and sync one generation's service using configured runtimes."""
    from .config import RUNTIME_DOCKER, RUNTIME_QEMU
    from .runtime.docker_runtime import DockerRuntime
    from .service import WorkspaceService

    runtimes: dict[str, object] = {}
    enabled = settings.enabled_runtime_list
    if RUNTIME_DOCKER in enabled:
        runtimes[RUNTIME_DOCKER] = DockerRuntime(base_url=settings.docker_socket)
        if log is not None:
            log.info("runtime_enabled", runtime=RUNTIME_DOCKER)
    if RUNTIME_QEMU in enabled:
        from .runtime.qemu_runtime import QemuRuntime

        runtimes[RUNTIME_QEMU] = QemuRuntime(settings=settings)
        if log is not None:
            log.info("runtime_enabled", runtime=RUNTIME_QEMU)
    if not runtimes:
        raise RuntimeError("No enabled workspace runtimes")
    service = WorkspaceService(runtimes, settings)
    await service.sync_from_runtime()
    return service


@app.command()
def stage_generation(
    source: Annotated[
        Path,
        typer.Argument(help="Local runner source directory containing src/"),
    ],
    version: Annotated[
        str,
        typer.Argument(help="Immutable generation identifier, e.g. release-2026-10-16"),
    ],
    state_dir: Annotated[
        Path | None,
        typer.Option("--state-dir", help="Runner state directory"),
    ] = None,
    activate: Annotated[
        bool,
        typer.Option("--activate/--stage-only", help="Atomically request activation"),
    ] = True,
) -> None:
    """Stage immutable runner code locally; requires supervised mode to activate."""
    from .supervisor import stage_generation as _stage

    settings = RunnerSettings()
    selected_state = str(state_dir or settings.state_dir)
    digest = _stage(source, selected_state, version, activate=activate)
    typer.echo(f"Staged generation {version} sha256:{digest}")
    if activate:
        typer.echo("Activation requested. It is applied only by supervised mode.")


@app.command()
def upgrade_status(
    state_dir: Annotated[
        Path | None,
        typer.Option("--state-dir", help="Runner state directory"),
    ] = None,
) -> None:
    """Show local supervisor generations and session-drain state."""
    import json

    from .supervisor import read_supervisor_status

    settings = RunnerSettings()
    typer.echo(
        json.dumps(
            read_supervisor_status(str(state_dir or settings.state_dir)), indent=2
        )
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
        if settings.supervised_mode:
            from .supervisor import RunnerSupervisor

            await RunnerSupervisor(settings).run()
            return

        from .interfaces.websocket import WebSocketInterface

        service = await build_workspace_service(settings, log)
        ws_interface = WebSocketInterface(service, settings)

        log.info(
            "runner_starting",
            backend_url=settings.backend_url,
            runtimes=settings.enabled_runtime_list,
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
    if (
        len(sys.argv) == 1
        or sys.argv[1].startswith("--")
        and sys.argv[1] not in {"--help", "--version"}
    ):
        app(args=["serve", *sys.argv[1:]])
    else:
        app()


if __name__ == "__main__":
    main()
