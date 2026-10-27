import { describe, expect, it, vi } from "vitest";
import type { AgentActivitySnapshot, OpenCuriaClient } from "@opencuria/client";
import { NotificationController } from "./controller";
import type { NativeNotifications } from "./native";
import type { LiveActivityHandle } from "./liveActivity";
import type { NotificationTarget } from "./target";

const deviceId = "11111111-1111-4111-8111-111111111111";
const organizationId = "22222222-2222-4222-8222-222222222222";
const row = {
  sessionId: "33333333-3333-4333-8333-333333333333",
  workspaceId: "44444444-4444-4444-8444-444444444444",
  title: "Build feature",
  workspaceName: "Workspace",
  phase: "running" as const,
};
const snapshot: AgentActivitySnapshot = {
  deviceId,
  organizationId,
  activeCount: 1,
  activities: [row],
  updatedAt: new Date().toISOString(),
};

function setup(
  preferences = { deviceId, notifications: false, liveActivities: false },
) {
  const listeners: Record<string, (...args: any[]) => void> = {};
  const cleanups: Array<ReturnType<typeof vi.fn>> = [];
  const listen = (name: string, listener: (...args: any[]) => void) => {
    listeners[name] = listener;
    const cleanup = vi.fn();
    cleanups.push(cleanup);
    return cleanup;
  };
  const api = {
    capabilities: vi
      .fn()
      .mockResolvedValue({ ios: true, android: true, live_activities: true }),
    registerDevice: vi.fn().mockResolvedValue({ id: deviceId }),
    unregisterDevice: vi.fn().mockResolvedValue(undefined),
    registerActivity: vi.fn().mockResolvedValue(undefined),
    activity: vi.fn().mockResolvedValue(snapshot),
  };
  const storage = {
    load: vi.fn().mockResolvedValue({ ...preferences }),
    save: vi.fn().mockResolvedValue(undefined),
  };
  const native = {
    platform: "ios",
    appId: "com.opencuria.mobile.dev",
    environment: vi.fn().mockResolvedValue("sandbox"),
    available: true,
    isActive: vi.fn().mockReturnValue(true),
    permission: vi.fn().mockResolvedValue(true),
    token: vi.fn().mockResolvedValue("device-token-a"),
    reset: vi.fn().mockResolvedValue(undefined),
    onToken: vi.fn((fn) => listen("device-token", fn)),
    onActivity: vi.fn((fn) => listen("foreground", fn)),
    onResponse: vi.fn((fn) => listen("response", fn)),
  } satisfies NativeNotifications;
  const handle = {
    id: "activity-one",
    update: vi.fn().mockResolvedValue(undefined),
    end: vi.fn().mockResolvedValue(undefined),
    token: vi.fn().mockResolvedValue("activity-token-a"),
    onToken: vi.fn((fn) => listen("activity-token", fn)),
  } satisfies LiveActivityHandle;
  let instances: LiveActivityHandle[] = [];
  const live = {
    available: true,
    instances: vi.fn(async () => instances),
    start: vi.fn(async () => {
      instances = [handle];
      return handle;
    }),
  };
  const client = {
    notifications: api,
    realtime: { on: vi.fn((name, fn) => listen(name, fn)) },
  } as unknown as OpenCuriaClient;
  const controller = new NotificationController(
    client,
    organizationId,
    storage,
    native,
    live,
  );
  return {
    controller,
    api,
    storage,
    native,
    live,
    handle,
    listeners,
    cleanups,
    setInstances: (value: LiveActivityHandle[]) => {
      instances = value;
    },
  };
}

describe("native notification lifecycle", () => {
  it("does not re-register in a loop when the native token read emits a token event", async () => {
    const test = setup({
      deviceId,
      notifications: true,
      liveActivities: false,
    });
    test.native.token.mockImplementation(async () => {
      if (test.native.token.mock.calls.length < 5)
        test.listeners["device-token"]!("device-token-a");
      return "device-token-a";
    });
    await test.controller.start();
    await Promise.resolve();
    await Promise.resolve();
    expect(test.native.token).toHaveBeenCalledOnce();
    expect(test.api.registerDevice).toHaveBeenCalledOnce();
    test.controller.dispose();
  });
  it("retries a failed secure preference read before registration", async () => {
    const test = setup();
    test.storage.load.mockRejectedValueOnce(
      new Error("SecureStore temporarily unavailable"),
    );
    await test.controller.start();
    expect(test.controller.getSnapshot().error).toMatch(/SecureStore/);
    expect(test.api.registerDevice).not.toHaveBeenCalled();
    await test.controller.refresh();
    expect(test.storage.load).toHaveBeenCalledTimes(2);
    expect(test.api.registerDevice).toHaveBeenCalledOnce();
    expect(test.native.onResponse).toHaveBeenCalledOnce();
    test.controller.dispose();
  });
  it("starts once without prompting, and enables notifications only after explicit consent", async () => {
    const test = setup();
    await Promise.all([test.controller.start(), test.controller.start()]);
    expect(test.native.onToken).toHaveBeenCalledTimes(1);
    expect(test.native.permission).toHaveBeenCalledWith(false);
    expect(test.native.token).not.toHaveBeenCalled();
    await test.controller.setEnabled("notifications", true);
    expect(test.native.permission).toHaveBeenLastCalledWith(true);
    expect(test.api.registerDevice).toHaveBeenLastCalledWith(
      deviceId,
      expect.objectContaining({
        notifications_enabled: true,
        push_token: "device-token-a",
      }),
    );
    expect(test.controller.getSnapshot().notifications).toBe(true);
    test.controller.dispose();
  });

  it("rolls back denied permission and does not create an enabled destination", async () => {
    const test = setup();
    await test.controller.start();
    test.native.permission.mockResolvedValue(false);
    await test.controller.setEnabled("notifications", true);
    expect(test.controller.getSnapshot().notifications).toBe(false);
    expect(test.controller.getSnapshot().error).toMatch(/device settings/);
    expect(
      test.api.registerDevice.mock.calls.every(
        ([, data]) => !data.notifications_enabled,
      ),
    ).toBe(true);
    expect(test.storage.save).toHaveBeenLastCalledWith(
      expect.objectContaining({ notifications: false }),
    );
    test.controller.dispose();
  });

  it("does not register an opt-in if SecureStore cannot persist it", async () => {
    const test = setup();
    await test.controller.start();
    test.storage.save.mockRejectedValueOnce(new Error("SecureStore failed"));
    await test.controller.setEnabled("notifications", true);
    expect(test.controller.getSnapshot().notifications).toBe(false);
    expect(test.api.registerDevice).toHaveBeenCalledTimes(1);
    test.controller.dispose();
  });

  it("restores running Live Activities without notification permission and observes token replacements", async () => {
    const test = setup({
      deviceId,
      notifications: false,
      liveActivities: true,
    });
    test.native.permission.mockResolvedValue(false);
    await test.controller.start();
    expect(test.live.start).toHaveBeenCalledWith(snapshot);
    expect(test.api.registerActivity).toHaveBeenLastCalledWith(
      deviceId,
      "activity-one",
      "activity-token-a",
    );
    test.handle.token.mockResolvedValue("activity-token-b");
    test.listeners["activity-token"]!("activity-token-b");
    await test.controller.refresh();
    expect(test.api.registerActivity).toHaveBeenLastCalledWith(
      deviceId,
      "activity-one",
      "activity-token-b",
    );
    test.controller.dispose();
    expect(
      test.cleanups.every((cleanup) => cleanup.mock.calls.length === 1),
    ).toBe(true);
  });

  it("reconciles permission revocation and native device-token changes on foreground", async () => {
    const test = setup({
      deviceId,
      notifications: true,
      liveActivities: false,
    });
    await test.controller.start();
    test.native.token.mockResolvedValue("device-token-b");
    test.listeners["device-token"]!();
    await test.controller.refresh();
    expect(test.api.registerDevice).toHaveBeenLastCalledWith(
      deviceId,
      expect.objectContaining({ push_token: "device-token-b" }),
    );
    test.native.permission.mockResolvedValue(false);
    await test.controller.refresh();
    expect(test.api.registerDevice).toHaveBeenLastCalledWith(
      deviceId,
      expect.objectContaining({ notifications_enabled: false, push_token: "" }),
    );
    expect(test.controller.getSnapshot().permissionGranted).toBe(false);
    test.controller.dispose();
  });

  it("accepts only taps for the current installation and organization", async () => {
    const test = setup();
    await test.controller.start();
    const target: NotificationTarget = {
      deviceId,
      organizationId,
      workspaceId: row.workspaceId,
      sessionId: row.sessionId,
    };
    test.listeners["response"]!({ ...target, deviceId: "other" });
    test.listeners["response"]!({ ...target, organizationId: "other" });
    expect(test.controller.getSnapshot().target).toBeNull();
    test.listeners["response"]!(target);
    expect(test.controller.getSnapshot().target).toEqual(target);
    test.controller.consumeTarget();
    expect(test.controller.getSnapshot().target).toBeNull();
    test.controller.dispose();
  });

  it("recovers an offline startup and removes stale or duplicate Live Activities", async () => {
    const test = setup({
      deviceId,
      notifications: false,
      liveActivities: true,
    });
    test.api.capabilities.mockRejectedValueOnce(new Error("Offline"));
    await test.controller.start();
    expect(test.controller.getSnapshot().error).toBe("Offline");
    const duplicate = {
      ...test.handle,
      id: "duplicate",
      end: vi.fn().mockResolvedValue(undefined),
    };
    test.setInstances([test.handle, duplicate]);
    await test.controller.refresh();
    expect(duplicate.end).toHaveBeenCalledOnce();
    expect(test.live.start).not.toHaveBeenCalled();
    test.api.activity.mockResolvedValue({
      ...snapshot,
      activeCount: 0,
      activities: [],
    });
    await test.controller.refresh();
    expect(test.handle.end).toHaveBeenCalled();
    test.controller.dispose();
  });

  it("does not start a new Live Activity while backgrounded", async () => {
    const test = setup({
      deviceId,
      notifications: false,
      liveActivities: true,
    });
    test.native.isActive.mockReturnValue(false);
    await test.controller.start();
    expect(test.live.start).not.toHaveBeenCalled();
    test.controller.dispose();
  });

  it("revokes before logout and cannot be re-registered by a late listener", async () => {
    const test = setup({ deviceId, notifications: true, liveActivities: true });
    await test.controller.start();
    await test.controller.revoke();
    const count = test.api.registerDevice.mock.calls.length;
    test.listeners["device-token"]!();
    test.listeners["activity-token"]!("late-token");
    await test.controller.refresh();
    expect(test.api.unregisterDevice).toHaveBeenCalledWith(deviceId);
    expect(test.native.reset).toHaveBeenCalledOnce();
    expect(test.api.registerDevice).toHaveBeenCalledTimes(count);
    expect(test.storage.save).toHaveBeenLastCalledWith({
      deviceId,
      notifications: false,
      liveActivities: false,
    });
    expect(test.handle.end).toHaveBeenCalled();
    test.controller.dispose();
  });

  it("keeps revocation retryable if the server cannot confirm logout cleanup", async () => {
    const test = setup({
      deviceId,
      notifications: true,
      liveActivities: false,
    });
    await test.controller.start();
    test.api.unregisterDevice.mockRejectedValueOnce(new Error("offline"));
    await expect(test.controller.revoke()).rejects.toThrow(/Reconnect/);
    expect(test.storage.save).not.toHaveBeenCalled();
    await test.controller.revoke();
    expect(test.api.unregisterDevice).toHaveBeenCalledTimes(2);
    test.controller.dispose();
  });

  it("revokes even with local consent off after a lost opt-in and rollback response", async () => {
    const test = setup();
    await test.controller.start();
    test.api.registerDevice.mockRejectedValue(new Error("Connection lost"));
    await test.controller.setEnabled("notifications", true);
    expect(test.controller.getSnapshot().notifications).toBe(false);
    await test.controller.revoke();
    expect(test.api.unregisterDevice).toHaveBeenCalledWith(deviceId);
    expect(test.native.reset).toHaveBeenCalledOnce();
    test.controller.dispose();
  });
});
