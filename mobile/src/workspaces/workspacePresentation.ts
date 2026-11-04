/**
 * How workspaces read in the native list and detail screens: grouping, status
 * colors, subtitles, menus and resource formatting. Lifecycle rules come from
 * `@opencuria/chat-core`; this file only decides presentation.
 */
import {
  describeDiskUsage,
  formatStorageBytes,
  isDiskUsageStale,
} from "@opencuria/chat-core/workspaceDisk";
import {
  getWorkspaceActionAvailability,
  workspaceOperationLabel,
  workspaceStatusLabel,
  workspaceStatusTone,
  type WorkspaceActionAvailability,
  type WorkspaceStatusTone,
} from "@opencuria/chat-core/workspaceLifecycle";
import { formatTimeAgo } from "@opencuria/chat-core/conversationGroups";
import {
  versionLabel,
  workspaceRecreateOptions,
} from "@opencuria/chat-core/imageVersions";
import type { Workspace } from "@opencuria/client";
import type { NativeSymbol } from "../ui/controls.types";
import type { NativeMenuAction } from "../ui/NativeMenu";
import type { NativeFormTone } from "../ui/NativeForm.types";
import type { OpenCuriaTheme } from "../ui/theme";
import { tileColor } from "../ui/tileColors";

export type WorkspaceGroupId =
  | "running"
  | "stopped"
  | "unavailable"
  | "removed";

export interface WorkspaceGroup {
  id: WorkspaceGroupId;
  title: string;
  workspaces: Workspace[];
}

/** Everything the screens show about one workspace's state. */
export interface WorkspaceState {
  tone: WorkspaceStatusTone;
  label: string;
  busy: boolean;
  actions: WorkspaceActionAvailability;
  /** Persisted transition such as "Starting…", or null at rest. */
  operation: string | null;
}

export type WorkspaceActionId =
  | "open"
  | "details"
  | "start"
  | "stop"
  | "capture"
  | "reset"
  | "remove";

const GROUP_TITLES: Record<WorkspaceGroupId, string> = {
  running: "Running",
  stopped: "Stopped",
  unavailable: "Unavailable",
  removed: "Removed",
};

const GROUP_OF_TONE: Record<WorkspaceStatusTone, WorkspaceGroupId> = {
  running: "running",
  busy: "running",
  stopped: "stopped",
  attention: "unavailable",
  error: "unavailable",
  offline: "unavailable",
  removed: "removed",
};

export function workspaceState(workspace: Workspace): WorkspaceState {
  const operation = workspaceOperationLabel(workspace);
  const tone = workspaceStatusTone(workspace, operation);
  return {
    tone,
    label: workspaceStatusLabel(workspace, operation),
    busy:
      Boolean(operation && !workspace.intervention_required) ||
      (workspace.status === "running" &&
        workspace.runner_online &&
        workspace.has_active_session),
    actions: getWorkspaceActionAvailability(workspace),
    operation,
  };
}

/**
 * Running (and transitioning) workspaces first, then stopped, then those that
 * cannot run right now; removed ones only on request. Names sort A–Z.
 */
export function groupWorkspaces(
  workspaces: readonly Workspace[],
  {
    query = "",
    showRemoved = false,
  }: { query?: string; showRemoved?: boolean } = {},
): WorkspaceGroup[] {
  const needle = query.trim().toLowerCase();
  const groups = new Map<WorkspaceGroupId, Workspace[]>();
  for (const workspace of workspaces) {
    if (
      needle &&
      !workspace.name.toLowerCase().includes(needle) &&
      !workspace.id.toLowerCase().includes(needle)
    )
      continue;
    const group = GROUP_OF_TONE[workspaceState(workspace).tone];
    if (group === "removed" && !showRemoved) continue;
    groups.set(group, [...(groups.get(group) ?? []), workspace]);
  }
  return (["running", "stopped", "unavailable", "removed"] as const).flatMap(
    (id) => {
      const rows = groups.get(id);
      if (!rows?.length) return [];
      return [
        {
          id,
          title: GROUP_TITLES[id],
          workspaces: [...rows].sort((left, right) =>
            left.name.localeCompare(right.name, undefined, {
              sensitivity: "base",
            }),
          ),
        },
      ];
    },
  );
}

/** Workspaces whose chats can no longer be opened. */
const CLOSED_STATUSES = new Set(["deleted", "deleting", "pending_deletion"]);

/**
 * The new-chat workspace menu: the list's order, and a workspace's status
 * only when it is not simply running.
 */
export function workspacePickerActions(
  workspaces: readonly Workspace[],
  activeWorkspaceId: string | null,
): NativeMenuAction[] {
  return groupWorkspaces(workspaces).flatMap((group) =>
    group.workspaces
      .filter((workspace) => !CLOSED_STATUSES.has(workspace.status))
      .map((workspace) => {
        const state = workspaceState(workspace);
        return {
          id: workspace.id,
          title: workspace.name,
          subtitle: state.tone === "running" ? undefined : state.label,
          selected: workspace.id === activeWorkspaceId,
        };
      }),
  );
}

/** Tile color per status; muted tones stay neutral so problems stand out. */
export function workspaceTint(
  tone: WorkspaceStatusTone,
  theme: OpenCuriaTheme,
): string {
  switch (tone) {
    case "running":
      return tileColor("green", theme);
    case "busy":
      return theme.primary;
    case "attention":
      return tileColor("orange", theme);
    case "error":
      return tileColor("red", theme);
    default:
      return tileColor("gray", theme);
  }
}

export function workspaceSymbol(tone: WorkspaceStatusTone): NativeSymbol {
  return tone === "error" || tone === "attention"
    ? "exclamationmark.triangle.fill"
    : "server.rack";
}

export function statusTextTone(tone: WorkspaceStatusTone): NativeFormTone {
  if (tone === "error") return "danger";
  if (tone === "attention") return "warning";
  return "muted";
}

/** "name · v3", or the image name recorded for older workspaces. */
export function workspaceImageLabel(workspace: Workspace): string | null {
  return (
    versionLabel(workspace.base_image) ?? workspace.base_image_name ?? null
  );
}

/** Storage almost full, as a short warning; null when there is room. */
export function storageWarning(workspace: Workspace): string | null {
  const usage = workspace.disk_usage;
  if (!usage || workspace.status === "deleted") return null;
  const view = describeDiskUsage(usage);
  return view.level === "ok" ? null : `Storage ${view.percent}% full`;
}

/** One line under the name: status, image and any storage warning. */
export function workspaceSubtitle(
  workspace: Workspace,
  state: WorkspaceState = workspaceState(workspace),
): { text: string; tone: NativeFormTone } {
  const warning = storageWarning(workspace);
  const parts = [state.label, workspaceImageLabel(workspace), warning].filter(
    (part): part is string => Boolean(part),
  );
  return {
    text: parts.join(" · "),
    tone: warning ? "warning" : statusTextTone(state.tone),
  };
}

/** Storage meter of the detail screen; null before the first measurement. */
export function workspaceStorage(
  workspace: Workspace,
  now = Date.now(),
): { value: number; label: string; tone: NativeFormTone } | null {
  const usage = workspace.disk_usage;
  if (!usage || workspace.status === "deleted") return null;
  const view = describeDiskUsage(usage);
  const stale =
    workspace.status !== "running" || isDiskUsageStale(usage, now)
      ? ` · ${formatTimeAgo(usage.observed_at, now)} ago`
      : "";
  return {
    value: view.percent / 100,
    label: `${formatStorageBytes(view.freeBytes)} free of ${formatStorageBytes(view.capacityBytes)}${stale}`,
    tone:
      view.level === "critical"
        ? "danger"
        : view.level === "warning"
          ? "warning"
          : "muted",
  };
}

/** "Stops in 4 min" when the idle timer is about to stop a running workspace. */
export function autoStopLabel(
  workspace: Workspace,
  now = Date.now(),
): string | null {
  if (!workspace.auto_stop_at || workspace.status !== "running") return null;
  const remaining = new Date(workspace.auto_stop_at).getTime() - now;
  if (remaining <= 0 || remaining > 10 * 60 * 1000) return null;
  return `Stops in ${Math.max(1, Math.round(remaining / 60000))} min`;
}

export function formatMemory(megabytes: number): string {
  return megabytes >= 1024 && megabytes % 1024 === 0
    ? `${megabytes / 1024} GiB`
    : megabytes >= 1024
      ? `${(megabytes / 1024).toFixed(1)} GiB`
      : `${megabytes} MiB`;
}

export function formatDisk(gigabytes: number): string {
  return `${gigabytes} GiB`;
}

export function formatVcpus(count: number): string {
  return `${count} vCPU${count === 1 ? "" : "s"}`;
}

/** Memory steps: 512 MiB below 4 GiB, whole GiB above. */
export function memoryStep(megabytes: number): number {
  return megabytes < 4096 ? 512 : 1024;
}

/** Disk steps: 5 GiB below 100 GiB, 10 GiB above. */
export function diskStep(gigabytes: number): number {
  return gigabytes < 100 ? 5 : 10;
}

/** Title of the reset action: an update when a newer image version exists. */
export function resetActionTitle(workspace: Workspace): string {
  const update = workspaceRecreateOptions(workspace).find(
    (option) => option.kind === "update",
  );
  return update ? `Update to v${update.version ?? "?"}` : "Reset workspace";
}

/** Reset is offered for workspaces based on a tracked image, as in the webapp. */
export function canRecreate(workspace: Workspace): boolean {
  return (
    Boolean(workspace.base_image || workspace.pending_base_image) &&
    workspace.runner_online &&
    ["running", "stopped", "failed"].includes(workspace.status)
  );
}

/** The full action menu of a workspace row, as in the webapp's "…" menu. */
export function workspaceMenuActions(
  workspace: Workspace,
  state: WorkspaceState,
  { canOpenChat }: { canOpenChat: boolean },
): NativeMenuAction[] {
  if (workspace.status === "deleted") return [];
  const { actions } = state;
  const running = workspace.status === "running" && workspace.runner_online;
  return [
    {
      id: "open",
      title: "Open chat",
      symbol: "bubble.left",
      disabled: !canOpenChat,
    },
    running
      ? {
          id: "stop",
          title: "Stop",
          symbol: "stop.fill",
          disabled: !actions.stop,
        }
      : {
          id: "start",
          title: "Start",
          symbol: "play.fill",
          disabled: !actions.start,
        },
    { id: "details", title: "Edit workspace", symbol: "pencil" },
    {
      id: "capture",
      title: "Capture image",
      symbol: "camera",
      disabled: !actions.capture,
    },
    ...(canRecreate(workspace)
      ? [
          {
            id: "reset",
            title: resetActionTitle(workspace),
            symbol: "arrow.counterclockwise",
            disabled: !actions.reset,
          },
        ]
      : []),
    {
      id: "remove",
      title:
        workspace.status === "delete_failed"
          ? "Retry removal"
          : "Remove workspace",
      symbol: "trash",
      destructive: true,
      disabled: !actions.remove,
    },
  ];
}
