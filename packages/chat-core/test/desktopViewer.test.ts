import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DESKTOP_STARTUP_MS,
  RELEASE_GRACE_MS,
  createDesktopViewerCoordinator,
  type DesktopStatus,
  type DesktopViewerIntent,
  type ViewerSession,
} from "../src/desktopViewer";
import {
  buildDesktopViewerUrl,
  computerUseRunId,
  createDesktopReconnectBackoff,
  workspaceDesktopSize,
} from "../src/desktopSurface";

const VIEWER = "11111111-2222-4333-8444-555555555555";

function heldStatus(revision: number, extra: Partial<DesktopStatus> = {}) {
  return {
    active: true,
    proxy_url: "/ws/desktop/ws/",
    viewer_held: true,
    computer_use_active: false,
    viewer_lease_state: "held",
    revision,
    epoch: "epoch-1",
    ...extra,
  } satisfies DesktopStatus;
}

function transport(status: (intent: number) => DesktopStatus) {
  let revision = 0;
  const calls: Array<{ action: string; intent?: DesktopViewerIntent }> = [];
  return {
    calls,
    port: {
      start: vi.fn(async (_id: string, intent: DesktopViewerIntent) => {
        revision = intent.intent_revision;
        calls.push({ action: "start", intent });
        return { task_id: "start" };
      }),
      stop: vi.fn(async (_id: string, intent: DesktopViewerIntent) => {
        calls.push({ action: "stop", intent });
        return { task_id: "stop" };
      }),
      renew: vi.fn(async (_id: string, intent: DesktopViewerIntent) => {
        calls.push({ action: "renew", intent });
        return status(revision);
      }),
      status: vi.fn(async () => {
        calls.push({ action: "status" });
        return status(revision);
      }),
    },
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) await Promise.resolve();
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("desktop viewer coordinator", () => {
  it("starts an intent and exposes an immutable ready snapshot", async () => {
    const { port, calls } = transport((revision) => heldStatus(revision));
    const coordinator = createDesktopViewerCoordinator({
      transport: port,
      viewerClientId: VIEWER,
    });
    const listener = vi.fn();
    coordinator.subscribe("ws", listener);
    coordinator.retain("ws", {});
    const before = coordinator.snapshot("ws");
    expect(before).toBe(coordinator.snapshot("ws"));
    expect(Object.isFrozen(before)).toBe(true);

    await coordinator.acquire("ws");
    await settle();

    const snapshot = coordinator.snapshot("ws");
    expect(snapshot).not.toBe(before);
    expect(snapshot).toMatchObject({
      ready: true,
      wanted: true,
      connecting: false,
      leaseState: "held",
      route: "/ws/desktop/ws/",
      startupFailed: false,
      revision: 1,
    });
    expect(calls[0]).toEqual({
      action: "start",
      intent: { viewer_client_id: VIEWER, intent_revision: 1 },
    });
    expect(coordinator.intent("ws")).toEqual({
      viewer_client_id: VIEWER,
      intent_revision: 1,
    });
    // Changes are coalesced per microtask.
    expect(listener).toHaveBeenCalled();
    expect(listener.mock.calls.length).toBeLessThan(5);
  });

  it("renews a held lease and re-acquires an expired one with a newer revision", async () => {
    let expireFirstIntent = false;
    const { port } = transport((revision) =>
      heldStatus(revision, {
        viewer_lease_state:
          expireFirstIntent && revision === 1 ? "expired" : "held",
      }),
    );
    const coordinator = createDesktopViewerCoordinator({ transport: port });
    coordinator.retain("ws", {});
    await coordinator.acquire("ws");
    await settle();

    await vi.advanceTimersByTimeAsync(45_000);
    expect(port.renew).toHaveBeenCalledWith(
      "ws",
      { viewer_client_id: coordinator.viewerClientId, intent_revision: 1 },
      expect.any(AbortSignal),
    );

    expireFirstIntent = true;
    await coordinator.refresh("ws");
    await settle();
    expect(port.start).toHaveBeenLastCalledWith(
      "ws",
      { viewer_client_id: coordinator.viewerClientId, intent_revision: 2 },
      expect.any(AbortSignal),
    );
    expect(port.start).toHaveBeenCalledTimes(2);
    expect(coordinator.snapshot("ws")).toMatchObject({
      ready: true,
      revision: 2,
    });
  });

  it("sends the tombstone after the release grace unless re-retained", async () => {
    const { port } = transport((revision) => heldStatus(revision));
    const coordinator = createDesktopViewerCoordinator({ transport: port });
    const first = {};
    coordinator.retain("ws", first);
    await coordinator.acquire("ws");
    await settle();

    void coordinator.release("ws", first);
    coordinator.retain("ws", {});
    await vi.advanceTimersByTimeAsync(RELEASE_GRACE_MS + 10);
    expect(port.stop).not.toHaveBeenCalled();

    const owner = {};
    const coordinator2 = createDesktopViewerCoordinator({ transport: port });
    coordinator2.retain("ws", owner);
    await coordinator2.acquire("ws");
    await settle();
    const released = coordinator2.release("ws", owner);
    await vi.advanceTimersByTimeAsync(RELEASE_GRACE_MS + 10);
    await expect(released).resolves.toBe(true);
    expect(port.stop).toHaveBeenCalledWith("ws", {
      viewer_client_id: coordinator2.viewerClientId,
      intent_revision: 1,
    });
    expect(coordinator2.snapshot("ws")).toMatchObject({
      wanted: false,
      ready: false,
      route: null,
    });
  });

  it("reports a startup that never becomes ready as failed", async () => {
    const { port } = transport((revision) =>
      heldStatus(revision, { active: false, proxy_url: null }),
    );
    const coordinator = createDesktopViewerCoordinator({ transport: port });
    coordinator.retain("ws", {});
    await coordinator.acquire("ws");
    await vi.advanceTimersByTimeAsync(DESKTOP_STARTUP_MS + 1_000);
    const snapshot = coordinator.snapshot("ws");
    expect(snapshot.startupFailed).toBe(true);
    expect(snapshot.ready).toBe(false);
    expect(snapshot.route).toBeNull();
    expect(snapshot.error).toMatch(/could not be confirmed/);
  });

  it("keeps the route through a transient status outage", async () => {
    let fail = false;
    const { port } = transport((revision) => {
      if (fail) throw new Error("offline");
      return heldStatus(revision);
    });
    const coordinator = createDesktopViewerCoordinator({ transport: port });
    coordinator.retain("ws", {});
    await coordinator.acquire("ws");
    await settle();
    fail = true;
    await coordinator.refresh("ws");
    await settle();
    const snapshot = coordinator.snapshot("ws");
    expect(snapshot.ready).toBe(false);
    expect(snapshot.error).toBe("offline");
    expect(snapshot.route).toBe("/ws/desktop/ws/");
  });

  it("reconnects: restores a wanted intent or confirms a mounted viewer", async () => {
    const { port } = transport((revision) => heldStatus(revision));
    const coordinator = createDesktopViewerCoordinator({ transport: port });
    coordinator.retain("ws", {});
    await coordinator.acquire("ws");
    await settle();

    await expect(coordinator.reconnect("ws", true)).resolves.toBe(true);
    expect(port.renew).toHaveBeenCalledTimes(1);

    const start = vi.fn(async () => undefined);
    await expect(coordinator.reconnect("ws", false, start)).resolves.toBe(
      false,
    );
    expect(start).toHaveBeenCalledTimes(1);

    await coordinator.close("ws");
    start.mockClear();
    await coordinator.reconnect("ws", false, start);
    expect(start).not.toHaveBeenCalled();
  });

  it("lets adapters wrap session records, e.g. with Vue's reactive", async () => {
    const wrapped: ViewerSession[] = [];
    const { port } = transport((revision) => heldStatus(revision));
    const coordinator = createDesktopViewerCoordinator({
      transport: port,
      track: (session) => {
        wrapped.push(session);
        return session;
      },
    });
    const session = coordinator.session("ws");
    expect(wrapped).toEqual([session]);
    coordinator.retain("ws", {});
    await coordinator.acquire("ws");
    await settle();
    expect(session.leaseState).toBe("held");
    expect(coordinator.session("ws")).toBe(session);
  });
});

describe("desktop surface rules", () => {
  it("opens the viewer with a ticket and the viewer intent only", () => {
    const url = new URL(
      buildDesktopViewerUrl("https://curia.test", "/ws/desktop/ws/", "t:k", {
        viewer_client_id: VIEWER,
        intent_revision: 4,
      }),
    );
    expect(url.origin + url.pathname).toBe("https://curia.test/ws/desktop/ws/");
    expect(url.searchParams.get("ticket")).toBe("t:k");
    expect(url.searchParams.get("resize")).toBe("scale");
    expect(url.searchParams.get("viewer_client_id")).toBe(VIEWER);
    expect(url.searchParams.get("intent_revision")).toBe("4");
    // The proxy derives the WebSocket path; no credential is ever in a URL.
    expect(url.searchParams.has("path")).toBe(false);
    expect(url.searchParams.has("token")).toBe(false);
  });

  it("falls back to the default framebuffer", () => {
    expect(workspaceDesktopSize({ desktop_width: 0 })).toEqual({
      width: 1920,
      height: 1080,
    });
  });

  it("identifies Computer Use subtasks only", () => {
    expect(
      computerUseRunId({
        agent: "ComputerUse",
        subtask_id: "s",
        child_session_id: "c",
      }),
    ).toBe("c");
    expect(computerUseRunId({ agent: "computeruse", subtask_id: "s" })).toBe(
      "s",
    );
    expect(computerUseRunId({ agent: "general", subtask_id: "s" })).toBeNull();
  });

  it("retries a lost viewer three times before giving up", async () => {
    const retry = vi.fn();
    const exhausted = vi.fn();
    const backoff = createDesktopReconnectBackoff(retry, exhausted);
    for (const delay of [1_000, 2_000, 4_000]) {
      backoff.schedule();
      await vi.advanceTimersByTimeAsync(delay);
    }
    backoff.schedule();
    expect(retry).toHaveBeenCalledTimes(3);
    expect(exhausted).toHaveBeenCalledTimes(1);
  });
});
