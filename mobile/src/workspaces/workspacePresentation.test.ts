import { describe, expect, it } from "vitest";
import type { Workspace } from "@opencuria/client";
import {
  autoStopLabel,
  formatMemory,
  groupWorkspaces,
  memoryStep,
  workspaceMenuActions,
  workspacePickerActions,
  workspaceState,
  workspaceStorage,
  workspaceSubtitle,
} from "./workspacePresentation";

const GIB = 1024 ** 3;

function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: overrides.name ?? "ws",
    name: "ws",
    status: "running",
    runner_id: "runner",
    active_operation: null,
    runner_online: true,
    has_active_session: false,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...overrides,
  };
}

describe("workspace list presentation", () => {
  it("groups running, stopped and unavailable workspaces and hides removed ones", () => {
    const rows = [
      workspace({ name: "zeta" }),
      workspace({
        name: "Alpha",
        active_operation: "starting",
        status: "stopped",
      }),
      workspace({ name: "idle", status: "stopped" }),
      workspace({ name: "broken", status: "failed" }),
      workspace({ name: "offline", runner_online: false }),
      workspace({ name: "gone", status: "deleted" }),
    ];
    expect(
      groupWorkspaces(rows).map((group) => [
        group.id,
        group.workspaces.map((item) => item.name),
      ]),
    ).toEqual([
      ["running", ["Alpha", "zeta"]],
      ["stopped", ["idle"]],
      ["unavailable", ["broken", "offline"]],
    ]);
    expect(groupWorkspaces(rows, { showRemoved: true }).at(-1)?.id).toBe(
      "removed",
    );
    expect(
      groupWorkspaces(rows, { query: "ZET" }).flatMap((group) =>
        group.workspaces.map((item) => item.name),
      ),
    ).toEqual(["zeta"]);
  });

  it("summarizes status, image and a storage warning in one line", () => {
    const row = workspace({
      base_image: {
        id: "v3",
        line_kind: "captured",
        line_id: "image",
        name: "node-dev",
        version: 3,
        message: "",
        status: "ready",
        latest_id: "v3",
        latest_version: 3,
        update_available: false,
      },
      disk_usage: {
        used_bytes: 46 * GIB,
        free_bytes: 4 * GIB,
        total_bytes: 51 * GIB,
        observed_at: new Date().toISOString(),
      },
    });
    expect(workspaceSubtitle(row)).toEqual({
      text: "Running · node-dev · v3 · Storage 92% full",
      tone: "warning",
    });
    expect(workspaceStorage(row)).toMatchObject({
      value: 0.92,
      label: "4.0 GiB free of 50.0 GiB",
      tone: "warning",
    });
  });

  it("offers open workspaces for a new chat, with their status when not running", () => {
    const rows = [
      workspace({ name: "zeta" }),
      workspace({ name: "Alpha", has_active_session: true }),
      workspace({ name: "idle", status: "stopped" }),
      workspace({ name: "leaving", status: "deleting" }),
      workspace({ name: "gone", status: "deleted" }),
    ];
    expect(workspacePickerActions(rows, "zeta")).toEqual([
      { id: "Alpha", title: "Alpha", subtitle: "Busy", selected: false },
      { id: "zeta", title: "zeta", subtitle: undefined, selected: true },
      { id: "idle", title: "idle", subtitle: "Stopped", selected: false },
    ]);
  });

  it("marks transitions and busy chats as busy", () => {
    expect(
      workspaceState(workspace({ active_operation: "stopping" })),
    ).toMatchObject({
      label: "Stopping…",
      busy: true,
      tone: "busy",
    });
    expect(
      workspaceState(workspace({ has_active_session: true })),
    ).toMatchObject({
      label: "Busy",
      busy: true,
    });
  });

  it("offers the webapp's actions and disables those the workspace cannot take", () => {
    const running = workspace();
    const actions = workspaceMenuActions(running, workspaceState(running), {
      canOpenChat: true,
    });
    expect(
      actions.map((action) => [action.id, Boolean(action.disabled)]),
    ).toEqual([
      ["open", false],
      ["stop", false],
      ["details", false],
      ["capture", false],
      ["remove", false],
    ]);
    const stopped = workspace({ status: "stopped", credentials_present: true });
    expect(
      workspaceMenuActions(stopped, workspaceState(stopped), {
        canOpenChat: true,
      })
        .filter((action) => action.disabled)
        .map((action) => action.id),
    ).toEqual(["capture"]);
  });

  it("announces an imminent idle stop and formats resources", () => {
    const now = Date.parse("2026-11-01T10:00:00Z");
    expect(
      autoStopLabel(workspace({ auto_stop_at: "2026-11-01T10:04:00Z" }), now),
    ).toBe("Stops in 4 min");
    expect(
      autoStopLabel(workspace({ auto_stop_at: "2026-11-01T11:00:00Z" }), now),
    ).toBeNull();
    expect(formatMemory(4096)).toBe("4 GiB");
    expect(formatMemory(1536)).toBe("1.5 GiB");
    expect(memoryStep(2048)).toBe(512);
    expect(memoryStep(8192)).toBe(1024);
  });
});
