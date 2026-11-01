"""Explicit in-memory transport adapter for persistent executor contract tests."""

from functools import partial
from types import SimpleNamespace

from src.journal import COMMANDS, OperationExecutor


def make_executor(socket, journal, reconciler=None, observer=None):
    """Wire fake transport callbacks and typed handlers without modifying emit."""
    handlers = dict(socket.handlers["/"])

    async def execute(event, data):
        return await handlers[event](data)

    executor = OperationExecutor(
        journal,
        SimpleNamespace(execute=execute),
        socket.emit,
        reconciler=reconciler,
        observer=observer,
        is_connected=lambda: getattr(socket, "connected", True),
    )
    for event in COMMANDS:
        socket.on(event, partial(executor.execute, event))
    socket.on("operation:ack", executor.acknowledge)
    socket.on("operation:inspect", executor.inspect)
    socket.on("operation:seal", executor.seal)
    socket.on("operation:inspect_request", executor.inspect_request)
    return executor


def make_interface(service, settings):
    """Construct the same application graph used by the runner entry point."""
    from src.application import RunnerApplication

    return RunnerApplication(service, settings).interface
