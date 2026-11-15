"""Exercise desktop actor lifetimes through their injected resource boundary."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from apps.harness.mcp_client.scopes import DesktopAgentScopes, DesktopScopeResources
from apps.harness.tools.base import ToolError, ToolRegistry


async def test_resource_factory_runs_once_and_tears_down_in_its_opening_task() -> None:
    opened = []
    closed = []

    @asynccontextmanager
    async def open_runtime() -> AsyncIterator[DesktopScopeResources]:
        owner = asyncio.current_task()
        opened.append(owner)
        resources = DesktopScopeResources(ToolRegistry())
        try:
            yield resources
        finally:
            assert asyncio.current_task() is owner
            resources.recordings.append("/workspace/child.mp4")
            closed.append(owner)

    scopes = DesktopAgentScopes(open_runtime)
    try:
        first, second = await asyncio.gather(
            scopes.tools("child"), scopes.tools("child")
        )
        assert first is second
        assert len(opened) == 1 and not closed
        await scopes.close("child")
        assert closed == opened
        assert scopes.recordings == ["/workspace/child.mp4"]
        with pytest.raises(ToolError, match="ended"):
            await scopes.tools("child")
        await scopes.close("child")
        assert closed == opened
    finally:
        await scopes.aclose()


async def test_failed_startup_is_reported_without_ending_other_actors() -> None:
    attempts = 0
    closed = []

    @asynccontextmanager
    async def open_runtime() -> AsyncIterator[DesktopScopeResources]:
        nonlocal attempts
        attempts += 1
        number = attempts
        owner = asyncio.current_task()
        try:
            if number == 1:
                raise RuntimeError("setup failed")
            yield DesktopScopeResources(ToolRegistry())
        finally:
            assert asyncio.current_task() is owner
            closed.append(number)

    scopes = DesktopAgentScopes(open_runtime)
    with pytest.raises(RuntimeError, match="setup failed"):
        await scopes.tools("failed")
    assert closed == [1]
    assert isinstance(await scopes.tools("healthy"), ToolRegistry)
    await scopes.close("healthy")
    assert closed == [1, 2]
    with pytest.raises(RuntimeError, match="setup failed"):
        await scopes.aclose()


async def test_close_during_startup_joins_the_factory_cleanup() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    @asynccontextmanager
    async def open_runtime() -> AsyncIterator[DesktopScopeResources]:
        owner = asyncio.current_task()
        try:
            started.set()
            await asyncio.Event().wait()
            yield DesktopScopeResources(ToolRegistry())
        finally:
            assert asyncio.current_task() is owner
            await asyncio.sleep(0)
            closed.set()

    scopes = DesktopAgentScopes(open_runtime)
    waiter = asyncio.create_task(scopes.tools("child"))
    try:
        await started.wait()
        with pytest.raises(asyncio.CancelledError):
            await scopes.close("child")
        assert closed.is_set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        await scopes.aclose()
