import React, { useEffect, useMemo, useState } from "react";
import { Alert } from "react-native";
import {
  UNLISTED_RUNNER_QEMU_LIMITS,
  isDesktopSizeValid,
  isWorkspaceSettingsChanged,
  qemuResourceLimits,
  workspaceSettingsForm,
  workspaceUpdatePayload,
  type WorkspaceSettingsForm,
  type WorkspaceSettingsInput,
} from "@opencuria/chat-core/workspaceCreation";
import { formatTimeAgo } from "@opencuria/chat-core/conversationGroups";
import type { OpenCuriaClient, Workspace } from "@opencuria/client";
import { NativeForm } from "../ui/NativeForm";
import type {
  NativeFormQuickAction,
  NativeFormRow,
  NativeFormSection,
} from "../ui/NativeForm.types";
import { NativeModal } from "../ui/primitives";
import { useTheme } from "../ui/theme";
import { showToast } from "../ui/toast";
import { CaptureImageSheet } from "./CaptureImageSheet";
import type { WorkspaceCatalog } from "./useWorkspaceCatalog";
import {
  desktopSizeRow,
  pluginCredentialSections,
  resourceRows,
  resourcesValid,
} from "./workspaceFormSections";
import {
  confirmRecreateWorkspace,
  confirmRemoveWorkspace,
  errorText,
  startWorkspace,
  stopWorkspace,
} from "./workspaceOperations";
import {
  autoStopLabel,
  canRecreate,
  formatDisk,
  formatMemory,
  formatVcpus,
  resetActionTitle,
  statusTextTone,
  workspaceImageLabel,
  workspaceState,
  workspaceStorage,
  workspaceSymbol,
  workspaceTint,
} from "./workspacePresentation";

export interface WorkspaceDetailSheetProps {
  workspace: Workspace | null;
  client: OpenCuriaClient;
  catalog: WorkspaceCatalog;
  organizationId: string | null;
  canOpenChat: boolean;
  onClose: () => void;
  onOpenChat: (workspace: Workspace) => void;
  /** Reload workspaces after a change the server accepted. */
  onChanged: () => Promise<void>;
}

function settingsInput(workspace: Workspace): WorkspaceSettingsInput {
  return {
    name: workspace.name,
    credential_ids: workspace.credential_ids ?? [],
    plugin_ids: workspace.plugin_ids ?? [],
    qemu_vcpus: workspace.qemu_vcpus ?? null,
    qemu_memory_mb: workspace.qemu_memory_mb ?? null,
    qemu_disk_size_gb: workspace.qemu_disk_size_gb ?? null,
    desktop_width: workspace.desktop_width,
    desktop_height: workspace.desktop_height,
  };
}

/**
 * One workspace: status and quick actions on top, then its settings (name,
 * plugins, credentials, resources) and removal, like an iOS settings page.
 */
export function WorkspaceDetailSheet(
  props: WorkspaceDetailSheetProps,
): React.JSX.Element | null {
  if (!props.workspace) return null;
  return (
    <DetailSheet
      key={props.workspace.id}
      {...props}
      workspace={props.workspace}
    />
  );
}

function DetailSheet({
  workspace,
  client,
  catalog,
  organizationId,
  canOpenChat,
  onClose,
  onOpenChat,
  onChanged,
}: WorkspaceDetailSheetProps & { workspace: Workspace }): React.JSX.Element {
  const { theme } = useTheme();
  const runner = catalog.runners.find(
    (entry) => entry.id === workspace.runner_id,
  );
  const limits = useMemo(
    () => qemuResourceLimits(runner, UNLISTED_RUNNER_QEMU_LIMITS),
    [runner],
  );
  const current = settingsInput(workspace);
  const [form, setForm] = useState<WorkspaceSettingsForm>(() =>
    workspaceSettingsForm(current, limits),
  );
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  // Presented above this sheet, so it opens from here rather than the list.
  const [capturing, setCapturing] = useState(false);
  const state = workspaceState(workspace);
  const editable = state.actions.edit && !saving;
  const dirty = isWorkspaceSettingsChanged(current, form, limits);

  // Follow server changes (and runner defaults once loaded) until edited.
  const serverKey = JSON.stringify(workspaceSettingsForm(current, limits));
  useEffect(() => {
    if (!touched) setForm(JSON.parse(serverKey) as WorkspaceSettingsForm);
  }, [serverKey, touched]);

  const edit = (next: Partial<WorkspaceSettingsForm>) => {
    setTouched(true);
    setForm((previous) => ({ ...previous, ...next }));
  };

  const selection = pluginCredentialSections({
    catalog,
    organizationId,
    pluginIds: form.pluginIds,
    credentialIds: form.credentialIds,
    onPluginIds: (pluginIds) => edit({ pluginIds }),
    onCredentialIds: (credentialIds) => edit({ credentialIds }),
    disabled: !editable,
  });
  const valid =
    Boolean(form.name.trim()) &&
    selection.valid &&
    resourcesValid(form, limits) &&
    isDesktopSizeValid(form.desktopWidth, form.desktopHeight);

  const save = async () => {
    if (!dirty || !valid || !editable) return;
    setSaving(true);
    try {
      const result = await client.workspaces.update(
        workspace.id,
        workspaceUpdatePayload(current, form, limits),
      );
      // Close first so the confirmation shows on the list beneath.
      onClose();
      if (
        result.credential_sync_status === "pending" ||
        result.credential_sync_status === "failed"
      )
        showToast({
          title: "Workspace saved",
          message:
            result.credential_sync_detail ||
            "Credentials are applied once the runner synchronizes them.",
          tone: "warning",
        });
      else if (result.active_operation === "restarting")
        showToast({
          title: "Workspace restarting",
          message: `${result.name} restarts to apply the new resources.`,
        });
      else
        showToast({
          title: "Workspace saved",
          tone: "success",
          message:
            form.credentialIds.join() !== current.credential_ids.join() &&
            workspace.status !== "running"
              ? "Credentials apply the next time it starts."
              : undefined,
        });
      void onChanged();
    } catch (cause) {
      showToast({
        title: "Could not save workspace",
        message: errorText(cause, "Please try again."),
        tone: "error",
      });
    } finally {
      setSaving(false);
    }
  };

  const close = () => {
    if (!dirty) {
      onClose();
      return;
    }
    Alert.alert(
      "Discard changes?",
      "Your changes to this workspace are not saved.",
      [
        { text: "Keep editing", style: "cancel" },
        { text: "Discard", style: "destructive", onPress: onClose },
      ],
    );
  };

  const after = (ok: boolean) => {
    if (ok) void onChanged();
  };
  const running = workspace.status === "running" && workspace.runner_online;
  const quickActions: NativeFormQuickAction[] = [
    {
      id: "open",
      title: "Chat",
      symbol: "bubble.left.fill",
      disabled: !canOpenChat,
    },
    running
      ? {
          id: "stop",
          title: "Stop",
          symbol: "stop.fill",
          disabled: !state.actions.stop,
        }
      : {
          id: "start",
          title: "Start",
          symbol: "play.fill",
          disabled: !state.actions.start,
        },
    {
      id: "capture",
      title: "Capture",
      symbol: "camera",
      disabled: !state.actions.capture,
    },
    ...(canRecreate(workspace)
      ? [
          {
            id: "reset",
            title: resetActionTitle(workspace).startsWith("Update")
              ? "Update"
              : "Reset",
            symbol: resetActionTitle(workspace).startsWith("Update")
              ? ("arrow.triangle.2.circlepath" as const)
              : ("arrow.counterclockwise" as const),
            disabled: !state.actions.reset,
          },
        ]
      : []),
  ];
  const onQuickAction = (id: string) => {
    if (id === "open") onOpenChat(workspace);
    else if (id === "start") void startWorkspace(client, workspace).then(after);
    else if (id === "stop") void stopWorkspace(client, workspace).then(after);
    else if (id === "capture") setCapturing(true);
    else if (id === "reset")
      void confirmRecreateWorkspace(client, workspace).then(after);
  };

  const storage = workspaceStorage(workspace);
  const stopsSoon = autoStopLabel(workspace);
  const problem = workspace.intervention_required
    ? workspace.runner_online
      ? "Checking an interrupted operation. Actions unlock after recovery."
      : "Runner offline. Recovery continues when it reconnects."
    : workspace.status === "delete_failed" && workspace.delete_last_error
      ? workspace.delete_last_error
      : workspace.status === "failed" && workspace.lifecycle_diagnostic
        ? workspace.lifecycle_diagnostic
        : null;
  const statusRows: NativeFormRow[] = [
    ...(problem
      ? [
          {
            kind: "note" as const,
            id: "problem",
            text: problem,
            tone: "danger" as const,
          },
        ]
      : []),
    {
      kind: "value",
      id: "image",
      label: "Image",
      value: workspaceImageLabel(workspace) ?? "—",
    },
    ...(workspace.base_image?.update_available && !workspace.pending_base_image
      ? [
          {
            kind: "value" as const,
            id: "update",
            label: "Update",
            value: `v${workspace.base_image.latest_version ?? "?"} available`,
            tone: "accent" as const,
          },
        ]
      : []),
    {
      kind: "value",
      id: "runner",
      label: "Runner",
      value: workspace.runner_online
        ? (runner?.name ?? "Online")
        : `${runner?.name ?? "Runner"} · offline`,
      tone: workspace.runner_online ? "muted" : "warning",
    },
    ...(storage
      ? [
          {
            kind: "gauge" as const,
            id: "storage",
            label: "Storage",
            value: storage.value,
            valueLabel: storage.label,
            tone: storage.tone,
          },
        ]
      : []),
    ...(workspace.last_activity_at
      ? [
          {
            kind: "value" as const,
            id: "activity",
            label: "Last active",
            value:
              formatTimeAgo(workspace.last_activity_at) === "now"
                ? "Just now"
                : `${formatTimeAgo(workspace.last_activity_at)} ago`,
          },
        ]
      : []),
  ];

  const sections: NativeFormSection[] = [
    {
      id: "header",
      rows: [
        {
          kind: "header",
          id: "workspace-header",
          title: workspace.name,
          subtitle: [state.label, stopsSoon].filter(Boolean).join(" · "),
          subtitleTone: statusTextTone(state.tone),
          symbol: workspaceSymbol(state.tone),
          tint: workspaceTint(state.tone, theme),
          busy: state.busy,
          actions: quickActions,
          onAction: onQuickAction,
        },
      ],
    },
    { id: "status", title: "Status", rows: statusRows },
    {
      id: "general",
      title: "Name",
      rows: [
        {
          kind: "text",
          id: "name",
          label: "Workspace name",
          placeholder: "Workspace name",
          value: form.name,
          onChange: (name) => edit({ name }),
          disabled: !editable,
        },
      ],
      footer: editable
        ? undefined
        : "Settings can be changed while the workspace is running or stopped and its runner is online.",
    },
    ...selection.sections,
    {
      id: "resources",
      title: "Resources",
      rows: [
        ...resourceRows({
          resources: form,
          limits,
          onChange: (resources) => edit(resources),
          disabled: !editable,
        }),
        desktopSizeRow({
          width: form.desktopWidth,
          height: form.desktopHeight,
          onChange: (desktopWidth, desktopHeight) =>
            edit({ desktopWidth, desktopHeight }),
          disabled: !editable,
        }),
      ],
      footer:
        "Changing CPU, memory or storage restarts a running workspace. The desktop size applies the next time the desktop starts.",
    },
    {
      id: "danger",
      rows: [
        {
          kind: "button",
          id: "remove",
          title:
            workspace.status === "delete_failed"
              ? "Retry removal"
              : "Remove workspace",
          symbol: "trash",
          destructive: true,
          disabled: !state.actions.remove,
          onPress: () => {
            void confirmRemoveWorkspace(client, workspace, onClose).then(
              (ok) => {
                if (ok) void onChanged();
              },
            );
          },
        },
      ],
      footer: `Created ${new Date(workspace.created_at).toLocaleDateString()}`,
    },
  ];

  return (
    <NativeModal
      visible
      onClose={close}
      title="Workspace"
      scrollable={false}
      swipeToDismiss={!dirty}
      primaryAction={{
        title: "Save",
        onPress: () => {
          void save();
        },
        disabled: !dirty || !valid || !editable,
        busy: saving,
        testID: "workspace-save",
      }}
      testID="workspace-detail-sheet"
    >
      <NativeForm sections={sections} />
      <CaptureImageSheet
        workspace={capturing ? workspace : null}
        client={client}
        catalog={catalog}
        onClose={() => setCapturing(false)}
        onCaptured={async () => {
          await Promise.allSettled([onChanged(), catalog.reload()]);
        }}
      />
    </NativeModal>
  );
}
