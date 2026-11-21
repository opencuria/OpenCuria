import React, { useMemo, useState } from "react";
import { Platform } from "react-native";
import type { OpenCuriaClient, Workspace } from "@opencuria/client";
import type {
  NativeFormItemRow,
  NativeFormSection,
  NativeFormSwipeAction,
} from "../ui/NativeForm.types";
import { errorRows, loadErrorState } from "../ui/formStates";
import { NativeModal } from "../ui/primitives";
import { CaptureImageSheet } from "./CaptureImageSheet";
import { CreateWorkspaceSheet } from "./CreateWorkspaceSheet";
import { WorkspaceDetailSheet } from "./WorkspaceDetailSheet";
import { useWorkspaceCatalog } from "./useWorkspaceCatalog";
import {
  confirmRecreateWorkspace,
  confirmRemoveWorkspace,
  startWorkspace,
  stopWorkspace,
} from "./workspaceOperations";
import {
  groupWorkspaces,
  workspaceMenuActions,
  workspaceState,
  workspaceStatus,
  workspaceSubtitle,
  workspaceSymbol,
} from "./workspacePresentation";

/** Room for Android's "New workspace" button; iOS shows it in the navigation bar. */
const NEW_WORKSPACE_INSET = Platform.OS === "android" ? 88 : 0;

export interface WorkspacesSheetProps {
  visible: boolean;
  onClose: () => void;
  client: OpenCuriaClient;
  organizationId: string | null;
  workspaces: Workspace[];
  /** The first load of the workspaces is still running. */
  loading: boolean;
  /** Why the workspaces could not be loaded, until a load succeeds. */
  error: string | null;
  activeWorkspaceId: string | null;
  /** Open the workspace's chats (a new chat when it can take one). */
  onOpenWorkspace: (workspace: Workspace) => void;
  /** Reload workspaces from the server. */
  onRefresh: () => Promise<void>;
}

/** Whether a workspace's chats can be opened (history stays readable when stopped). */
function canOpen(workspace: Workspace): boolean {
  return !["deleted", "pending_deletion", "deleting"].includes(
    workspace.status,
  );
}

/**
 * All workspaces of the organization: search, status groups, swipe and
 * context actions, and the sheets to create, edit and capture workspaces.
 */
export function WorkspacesSheet({
  visible,
  onClose,
  client,
  organizationId,
  workspaces,
  loading,
  error,
  activeWorkspaceId,
  onOpenWorkspace,
  onRefresh,
}: WorkspacesSheetProps): React.JSX.Element | null {
  const [query, setQuery] = useState("");
  const [retrying, setRetrying] = useState(false);
  const [showRemoved, setShowRemoved] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [captureId, setCaptureId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const catalog = useWorkspaceCatalog(client, visible);

  const detail = workspaces.find((item) => item.id === detailId) ?? null;
  const capturing = workspaces.find((item) => item.id === captureId) ?? null;
  const hasRemoved = workspaces.some((item) => item.status === "deleted");
  const groups = useMemo(
    () => groupWorkspaces(workspaces, { query, showRemoved }),
    [workspaces, query, showRemoved],
  );

  if (!visible) return null;

  const open = (workspace: Workspace) => {
    setQuery("");
    onOpenWorkspace(workspace);
  };
  const retry = () => {
    setRetrying(true);
    void onRefresh().finally(() => setRetrying(false));
  };
  const changed = (ok: boolean) => {
    if (ok) void onRefresh();
  };
  const act = (workspace: Workspace, id: string) => {
    if (id === "open") open(workspace);
    else if (id === "details") setDetailId(workspace.id);
    else if (id === "start")
      void startWorkspace(client, workspace).then(changed);
    else if (id === "stop") void stopWorkspace(client, workspace).then(changed);
    else if (id === "capture") setCaptureId(workspace.id);
    else if (id === "reset")
      void confirmRecreateWorkspace(client, workspace).then(changed);
    else if (id === "remove")
      void confirmRemoveWorkspace(client, workspace).then(changed);
  };

  const row = (workspace: Workspace): NativeFormItemRow => {
    const state = workspaceState(workspace);
    const subtitle = workspaceSubtitle(workspace, state);
    const running = workspace.status === "running" && workspace.runner_online;
    const leading: NativeFormSwipeAction[] =
      running && state.actions.stop
        ? [{ id: "stop", title: "Stop", symbol: "stop.fill", tone: "muted" }]
        : state.actions.start
          ? [
              {
                id: "start",
                title: "Start",
                symbol: "play.fill",
                tone: "success",
              },
            ]
          : [];
    const trailing: NativeFormSwipeAction[] = [
      ...(state.actions.remove
        ? [
            {
              id: "remove",
              title: "Remove",
              symbol: "trash" as const,
              destructive: true,
            },
          ]
        : []),
      ...(workspace.status === "deleted"
        ? []
        : [
            {
              id: "details",
              title: "Edit",
              symbol: "pencil" as const,
              tone: "muted" as const,
            },
          ]),
    ];
    const update =
      workspace.base_image?.update_available && !workspace.pending_base_image;
    return {
      kind: "item",
      id: workspace.id,
      testID: `workspace-row-${workspace.id}`,
      title: workspace.name,
      subtitle: subtitle.text,
      subtitleTone: subtitle.tone,
      status: workspaceStatus(state.tone),
      symbol: workspaceSymbol(state.tone),
      tile: true,
      selected: workspace.id === activeWorkspaceId,
      busy: state.busy,
      // An update cannot start while the workspace is busy.
      badge: update && !state.busy ? "Update" : undefined,
      disabled: !canOpen(workspace),
      onPress: canOpen(workspace) ? () => open(workspace) : undefined,
      onInfo:
        workspace.status === "deleted"
          ? undefined
          : () => setDetailId(workspace.id),
      infoLabel: `Details for ${workspace.name}`,
      actions: workspaceMenuActions(workspace, state, {
        canOpenChat: canOpen(workspace),
      }),
      leadingSwipe: leading,
      trailingSwipe: trailing,
      onAction: (id) => act(workspace, id),
    };
  };

  const sections: NativeFormSection[] = [
    // A failed refresh keeps the last list and says so above it.
    ...(error && workspaces.length
      ? [{ id: "error", rows: errorRows("workspaces", error, retry, retrying) }]
      : []),
    ...groups.map((group) => ({
      id: group.id,
      title: group.title,
      rows: group.workspaces.map(row),
    })),
  ];

  return (
    <NativeModal
      visible
      onClose={onClose}
      title="Workspaces"
      largeTitle
      search={{
        value: query,
        onChange: setQuery,
        placeholder: "Search workspaces",
      }}
      primaryAction={{
        title: "New workspace",
        symbol: "plus",
        onPress: () => setCreating(true),
        testID: "new-workspace",
      }}
      actions={
        hasRemoved
          ? [
              {
                id: "removed",
                title: "Show removed workspaces",
                selected: showRemoved,
              },
            ]
          : undefined
      }
      onAction={(id) => {
        if (id === "removed") setShowRemoved((value) => !value);
      }}
      testID="workspace-picker-sheet"
      form={{
        sections,
        loading: loading && !workspaces.length,
        onRefresh: async () => {
          await Promise.allSettled([onRefresh(), catalog.reload()]);
        },
        empty:
          error && !workspaces.length
            ? loadErrorState("workspaces", error, retry, retrying)
            : query.trim()
              ? {
                  title: "No results",
                  description: `Nothing matches “${query.trim()}”.`,
                  symbol: "magnifyingglass",
                }
              : {
                  title: "No workspaces yet",
                  description:
                    "Create a workspace to start working with an agent.",
                  symbol: "server.rack",
                  action: {
                    title: "New workspace",
                    symbol: "plus",
                    onPress: () => setCreating(true),
                    testID: "empty-new-workspace",
                  },
                },
        contentInsetBottom: NEW_WORKSPACE_INSET,
        testID: "workspace-list",
      }}
    >
      <WorkspaceDetailSheet
        workspace={detail}
        client={client}
        catalog={catalog}
        organizationId={organizationId}
        canOpenChat={Boolean(detail && canOpen(detail))}
        onClose={() => setDetailId(null)}
        onOpenChat={(workspace) => {
          setDetailId(null);
          open(workspace);
        }}
        onChanged={onRefresh}
      />
      <CreateWorkspaceSheet
        visible={creating}
        client={client}
        catalog={catalog}
        organizationId={organizationId}
        onClose={() => setCreating(false)}
        onCreated={async () => {
          setCreating(false);
          await onRefresh();
        }}
      />
      <CaptureImageSheet
        workspace={capturing}
        client={client}
        catalog={catalog}
        onClose={() => setCaptureId(null)}
        onCaptured={async () => {
          await Promise.allSettled([onRefresh(), catalog.reload()]);
        }}
      />
    </NativeModal>
  );
}
