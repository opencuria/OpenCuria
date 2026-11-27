import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Modal, Platform, View } from "react-native";
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
import type {
  NativeFormQuickAction,
  NativeFormRow,
  NativeFormSection,
} from "../ui/NativeForm.types";
import type { ModalPrimaryAction } from "../ui/ModalFrame.types";
import { useTheme } from "../ui/theme";
import { showToast } from "../ui/toast";
import { ToastHost } from "../ui/ToastHost";
import {
  SettingsNavigationContext,
  type SettingsNavigation,
} from "../settings/SettingsContext";
import { SettingsNavigator } from "../settings/SettingsNavigator";
import { SettingsPage } from "../settings/SettingsPage";
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
  workspaceStatus,
  workspaceStorage,
  workspaceSymbol,
  workspaceSystem,
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

/** Route of the pushed "Plugins & Credentials" page. */
const ACCESS_ROUTE = "access";

/**
 * One workspace: status and quick actions on top, then its settings and
 * removal, like an iOS settings page. Plugins and credentials live on a
 * pushed "Plugins & Credentials" page of the sheet's native navigation stack.
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
  const [path, setPath] = useState<string[]>([]);
  const { theme } = useTheme();
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

  const close = useCallback(() => {
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
  }, [dirty, onClose]);
  const navigation = useMemo<SettingsNavigation>(
    () => ({
      push: (next) =>
        setPath((current) =>
          current[current.length - 1] === next ? current : [...current, next],
        ),
      pop: () => setPath((current) => current.slice(0, -1)),
      replace: (next) => setPath((current) => [...current.slice(0, -1), next]),
      popToRoot: () => setPath([]),
      close,
      choose: () => undefined,
      present: () => undefined,
    }),
    [close],
  );

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
  const system = workspaceSystem(workspace);
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
            symbol: "exclamationmark.triangle" as const,
          },
        ]
      : []),
    {
      kind: "value",
      id: "image",
      label: "Image",
      value: workspaceImageLabel(workspace) ?? "—",
    },
    ...(system
      ? [
          {
            kind: "value" as const,
            id: "system",
            label: "System",
            value: system.text,
            tone: system.tone,
          },
        ]
      : []),
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
          status: workspaceStatus(state.tone),
          symbol: workspaceSymbol(state.tone),
          busy: state.busy,
          actions: quickActions,
          onAction: onQuickAction,
        },
      ],
    },
    { id: "status", title: "Status", rows: statusRows },
    {
      id: "general",
      rows: [
        {
          kind: "text",
          id: "name",
          label: "Name",
          placeholder: "Name, e.g. billing-service",
          value: form.name,
          onChange: (name) => edit({ name }),
          disabled: !editable,
        },
      ],
      footer: editable
        ? undefined
        : "Settings can be changed while the workspace is running or stopped and its runner is online.",
    },
    {
      id: "access",
      rows: [
        {
          kind: "link",
          id: "access-link",
          title: "Plugins & Credentials",
          symbol: "puzzlepiece.extension",
          subtitle: selection.summary
            ? (selection.summary.issue ?? selection.summary.text)
            : "Loading…",
          subtitleTone: selection.summary?.issue ? "warning" : "muted",
          busy: !selection.summary,
          route: ACCESS_ROUTE,
        },
      ],
    },
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

  const primaryAction: ModalPrimaryAction = {
    title: "Save",
    onPress: () => {
      void save();
    },
    disabled: !dirty || !valid || !editable,
    busy: saving,
    testID: "workspace-save",
  };

  return (
    <Modal
      visible
      statusBarTranslucent
      navigationBarTranslucent
      animationType="slide"
      presentationStyle="pageSheet"
      // Unsaved changes keep the sheet until the close button confirms.
      allowSwipeDismissal={!dirty}
      // Back pops the sub page on Android before it closes the sheet.
      onRequestClose={() =>
        Platform.OS === "android" && path.length ? navigation.pop() : close()
      }
    >
      <SettingsNavigationContext.Provider value={navigation}>
        <View
          accessibilityViewIsModal
          onAccessibilityEscape={path.length ? navigation.pop : close}
          testID="workspace-detail-sheet"
          style={{ flex: 1, backgroundColor: theme.background }}
        >
          <SettingsNavigator
            path={path}
            onPathChange={setPath}
            root={
              <SettingsPage
                root
                title="Workspace"
                sections={sections}
                primaryAction={primaryAction}
              />
            }
            renderRoute={(route) =>
              route === ACCESS_ROUTE ? (
                <SettingsPage
                  title="Plugins & Credentials"
                  sections={selection.sections}
                  primaryAction={primaryAction}
                  testID="workspace-access-page"
                />
              ) : null
            }
          />
        </View>
        <CaptureImageSheet
          workspace={capturing ? workspace : null}
          client={client}
          catalog={catalog}
          onClose={() => setCapturing(false)}
          onCaptured={async () => {
            await Promise.allSettled([onChanged(), catalog.reload()]);
          }}
        />
        {/* Inside the modal root, so VoiceOver can reach its toasts. */}
        <ToastHost />
      </SettingsNavigationContext.Provider>
    </Modal>
  );
}
