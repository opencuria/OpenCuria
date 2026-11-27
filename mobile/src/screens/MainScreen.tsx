import React, { useCallback, useEffect, useMemo, useState } from "react";
import { ActivityIndicator, StyleSheet, View } from "react-native";
import { KeyboardController } from "react-native-keyboard-controller";

import { isModelProviderConnected } from "@opencuria/chat-core/workspaceSetup";
import type { HarnessConversation } from "@opencuria/client";
import { WorkspaceChatView } from "../components/WorkspaceChatView";
import { Drawer } from "../ui/Drawer";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { WorkspaceSidebar } from "../components/WorkspaceSidebar";
import { useTheme } from "../ui/theme";
import { useSession } from "./SessionContext";
import { useNotifications } from "../notifications/NotificationContext";
import { setViewedNotificationSession } from "../notifications/native";
import { WorkspaceToolsSheet } from "../tools/WorkspaceToolsSheet";
import { WorkspacesSheet } from "../workspaces/WorkspacesSheet";
import { WorkspaceSetupGuide } from "../workspaces/WorkspaceSetupGuide";
import { workspacePickerActions } from "../workspaces/workspacePresentation";
import { ScheduledTaskSheet } from "../scheduledTasks/ScheduledTaskSheet";
import { ScheduledTasksSheet } from "../scheduledTasks/ScheduledTasksSheet";
import type { AgentCatalog } from "../scheduledTasks/scheduledTaskForm";
import { activeTaskCount } from "../scheduledTasks/scheduledTaskPresentation";
import { useScheduledTasks } from "../scheduledTasks/useScheduledTasks";
import { AppsSheet } from "../apps/AppsSheet";
import { PublishedAppCard } from "../apps/PublishedAppCard";
import { pendingAppCount } from "../apps/appPresentation";
import { useApps } from "../apps/useApps";
import { useAppsWidgetRequests } from "../widgets/AppsWidgetProvider";
import { showToast } from "../ui/toast";
import {
  isOrganizationAdmin,
  profileInitials,
  profileName,
} from "../settings/profile";
import { useSavedChatCleanup } from "../offline/useSavedChatCleanup";
import { SettingsScreen } from "../settings/SettingsScreen";
import { releaseGitControllers } from "../tools/git";
import { releaseTerminalSessions } from "../tools/terminal";
import {
  HIDDEN_WORKSPACE_STATES,
  errorMessage,
  useWorkspaceChat,
} from "../state/useWorkspaceChat";

/** Sheets that stay open offline; every other sheet needs the server. */
const OFFLINE_SHEETS = new Set(["drawer", "apps"]);

export function MainScreen(): React.JSX.Element {
  const notifications = useNotifications();
  const { theme } = useTheme();
  const {
    client: sessionClient,
    session: authSession,
    offline: sessionOffline,
    organizations,
    reconnecting,
    retry,
    signOut,
    selectOrganization,
    createOrganization,
    registerResource,
  } = useSession();
  const [notificationRetry, setNotificationRetry] = useState(0);
  /** Settings opens on this page next, e.g. to set up voice input. */
  const [settingsPath, setSettingsPath] = useState<string[]>([]);
  /** Counts Settings closes; the setup guide re-checks after each. */
  const [settingsClosed, setSettingsClosed] = useState(0);
  const [drawerError, setDrawerError] = useState<string | null>(null);
  const schedules = useScheduledTasks(
    sessionOffline ? null : sessionClient,
    `${sessionClient?.api.baseUrl ?? ""}:${authSession?.organizationId ?? ""}`,
  );

  // Options are read when called, so the callbacks may use later bindings.
  const chat = useWorkspaceChat({
    offlineSheets: OFFLINE_SHEETS,
    navigationBlocked: Boolean(notifications.state.target),
    onRetryNavigation: () => setNotificationRetry((revision) => revision + 1),
    onNavigationRequest: (request) => {
      if (!request.notificationTarget)
        notifications.controller?.consumeTarget();
    },
    onNavigated: (request) => {
      if (request.notificationTarget)
        notifications.controller?.consumeTarget(request.notificationTarget);
    },
    onForegroundRefresh: () => schedules.refresh(),
    onScheduledTask: (task) => schedules.upsert(task),
    onOpenSettings: (path) => openSettings(path),
  });
  const {
    client,
    session,
    user,
    offline,
    workspaces,
    workspace,
    workspaceId,
    conversations,
    sheet,
    setSheet,
    openSheet,
  } = chat;

  useSavedChatCleanup(user ? chat.accountScope : null);
  const apps = useApps(client, chat.accountScope, undefined, { offline });
  const widgetRequests = useAppsWidgetRequests();
  const renderPublishedApp = useCallback(
    (reference: Parameters<typeof PublishedAppCard>[0]["reference"]) => (
      <PublishedAppCard reference={reference} apps={apps} />
    ),
    [apps],
  );

  /** Opens Settings on a page, e.g. `["providers"]`. */
  const openSettings = (path: string[]) => {
    setSettingsPath(path);
    openSheet("profile");
  };

  // Terminal shells and Git state outlive the tools sheet, not the client.
  useEffect(() => {
    if (!client) return;
    const dispose = () => {
      releaseTerminalSessions(client);
      releaseGitControllers(client);
    };
    const unregister = registerResource(dispose);
    return () => {
      unregister();
      dispose();
    };
  }, [client, registerResource]);

  useEffect(() => {
    setViewedNotificationSession(chat.chat.activeSessionId);
    return () => setViewedNotificationSession(null);
  }, [chat.chat.activeSessionId]);

  // The home screen widget's More tile shows every app.
  const { appsRequested, consumeAppsRequest } = widgetRequests;
  useEffect(() => {
    if (!appsRequested) return;
    consumeAppsRequest();
    void KeyboardController.dismiss().catch(() => undefined);
    setSheet("apps");
  }, [appsRequested, consumeAppsRequest, setSheet]);

  const {
    setWorkspaces,
    setAttachments,
    requestNavigation,
    setError,
    navigationRevision,
  } = chat;
  const loading = chat.loading;
  useEffect(() => {
    const target = notifications.state.target;
    if (
      !target ||
      !client ||
      loading ||
      offline ||
      target.organizationId !== session?.organizationId
    )
      return;
    let active = true;
    const revision = navigationRevision.current;
    // Resolve through authenticated REST first. A push cannot grant access to
    // another account's workspace or make us silently open a different session.
    void Promise.all([
      client.workspaces.get(target.workspaceId),
      client.harness.listSessions(target.workspaceId),
    ])
      .then(([owner, sessions]) => {
        if (!active || revision !== navigationRevision.current) return;
        if (!sessions.some((item) => item.id === target.sessionId))
          throw Object.assign(
            new Error("This conversation is no longer available."),
            { status: 404 },
          );
        setWorkspaces((rows) =>
          rows.some((item) => item.id === owner.id) ? rows : [...rows, owner],
        );
        setSheet(null);
        setAttachments([]);
        requestNavigation(owner.id, {
          sessionId: target.sessionId,
          notificationTarget: target,
        });
      })
      .catch((cause: unknown) => {
        if (active && revision === navigationRevision.current) {
          const status =
            cause && typeof cause === "object" && "status" in cause
              ? cause.status
              : undefined;
          if (status === 403 || status === 404) {
            setError(
              "This notification's conversation is no longer available.",
            );
            notifications.controller?.consumeTarget(target);
          } else {
            setError(
              "Could not open this conversation. Check your connection and retry.",
            );
          }
        }
      });
    return () => {
      active = false;
    };
    // Runs again for a new target, connection or retry; the navigation it
    // starts must not cancel it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    notifications.state.target,
    notifications.controller,
    client,
    loading,
    offline,
    notificationRetry,
    session?.organizationId,
  ]);

  const openConversation = (row: HarnessConversation) => {
    const owner = workspaces.find((item) => item.id === row.workspace_id);
    // Offline, a saved chat opens even when its workspace was not saved.
    if (!owner && !offline) {
      setDrawerError("This conversation’s workspace is no longer available.");
      return;
    }
    setDrawerError(null);
    chat.openConversation(row);
  };

  /** A scheduled run's chat; its workspace may have been removed since. */
  const openRunChat = (runWorkspaceId: string, sessionId: string) => {
    const owner = workspaces.find((item) => item.id === runWorkspaceId);
    if (!owner || HIDDEN_WORKSPACE_STATES.has(owner.status)) {
      showToast({
        title: "Chat unavailable",
        message: "The workspace of this run is no longer available.",
        tone: "error",
      });
      return;
    }
    setAttachments([]);
    setSheet(null);
    setError(null);
    requestNavigation(owner.id, { sessionId });
  };

  const markAllRead = async () => {
    const running = new Set(
      workspaces
        .filter((item) => item.status === "running")
        .map((item) => item.id),
    );
    try {
      await Promise.all(
        conversations
          .filter((row) => row.unread && running.has(row.workspace_id))
          .map((row) => client!.harness.markRead(row.session_id)),
      );
      await chat.loadOverview(true);
    } catch (cause) {
      setDrawerError(
        errorMessage(cause, "Could not mark conversations as read."),
      );
    }
  };

  const profileTitle = user ? profileName(user) : "Account";
  const agentCatalog = useMemo<AgentCatalog>(
    () => ({
      engines: chat.engines,
      models: chat.models,
      engineModels: chat.engineModels,
      agentConfigs: chat.agentConfigs,
      recentModelIds: chat.recentModels.map((row) => row.model),
      error: chat.catalogError,
    }),
    [
      chat.engines,
      chat.models,
      chat.engineModels,
      chat.agentConfigs,
      chat.recentModels,
      chat.catalogError,
    ],
  );
  /** Until its first message, a new chat can still change workspace. */
  const newChatWorkspace =
    workspace &&
    !offline &&
    !chat.chat.activeSessionId &&
    !chat.navigation?.sessionId
      ? {
          name: workspace.name,
          actions: workspacePickerActions(workspaces, workspace.id),
          onSelect: chat.moveNewChat,
        }
      : undefined;
  /** Git, Desktop, Terminal and Files need the workspace's VM and runner. */
  const toolsAvailable = Boolean(
    !offline &&
    workspace &&
    workspace.status === "running" &&
    workspace.runner_online &&
    !chat.isTransitioning,
  );
  const subagent = chat.subagent;
  const switchWorkspaceAction: NativeMenuAction = {
    id: "workspace",
    title: "Switch workspace",
    symbol: "square.stack.3d.up",
  };
  const chatMenuActions: NativeMenuAction[] = offline
    ? []
    : subagent
      ? [
          // The subagent header has no tools button of its own.
          {
            id: "tools",
            title: "Git, Desktop, Terminal & Files",
            symbol: "arrow.triangle.branch",
            disabled: !toolsAvailable,
          },
          ...(subagent.rootId
            ? [
                {
                  id: "root",
                  title: "Back to main chat",
                  symbol: "arrow.uturn.backward",
                },
              ]
            : []),
          switchWorkspaceAction,
        ]
      : [
          switchWorkspaceAction,
          ...(chat.chat.activeSession
            ? [
                {
                  id: "rename",
                  title: "Rename chat",
                  symbol: "pencil",
                  disabled: chat.isTransitioning,
                },
              ]
            : []),
          ...(workspace
            ? [
                {
                  id: "schedule",
                  title: chat.draft.trim()
                    ? "Schedule this prompt"
                    : "New scheduled task",
                  symbol: "calendar.badge.clock",
                },
              ]
            : []),
          ...(chat.activeConversation
            ? [
                {
                  id: "delete",
                  title: "Delete chat",
                  symbol: "trash",
                  destructive: true,
                  disabled: chat.isTransitioning || chat.actionBusy,
                },
              ]
            : []),
        ];
  const chatMenuAction = (id: string) => {
    if (id === "workspace") {
      openSheet("workspaces");
    } else if (id === "tools") {
      openSheet("tools");
    } else if (id === "root" && subagent?.rootId) {
      chat.openRelatedChat(subagent.rootId);
    } else if (id === "schedule") {
      openSheet("schedule-prompt");
    } else if (id === "rename" && chat.chat.activeSession) {
      chat.rename(chat.chat.activeSession);
    } else if (id === "delete" && chat.activeConversation) {
      chat.confirmDelete(chat.activeConversation);
    }
  };

  if (!client || !user)
    return (
      <View style={[styles.center, { backgroundColor: theme.background }]}>
        <ActivityIndicator color={theme.primary} />
      </View>
    );

  const scopeKey = `${client.api.baseUrl}:${session?.organizationId ?? ""}`;
  return (
    <View style={styles.root}>
      <Drawer
        visible={sheet === "drawer"}
        onOpen={() => setSheet("drawer")}
        onClose={() =>
          setSheet((current) => (current === "drawer" ? null : current))
        }
        title={workspace?.name ?? "OpenCuria"}
        onCreate={chat.newChat}
        canCreate={chat.ready}
        onTools={() => openSheet("tools")}
        canOpenTools={toolsAvailable}
        menuActions={chatMenuActions}
        onMenuAction={chatMenuAction}
        detail={
          subagent
            ? {
                title: subagent.title,
                subtitle: subagent.subtitle,
                backTitle: subagent.backTitle,
                onBack: () => chat.openRelatedChat(subagent.parentId),
              }
            : undefined
        }
        navigation={
          <WorkspaceSidebar
            workspaces={workspaces}
            conversations={conversations}
            activeWorkspaceId={workspaceId}
            activeSessionId={chat.chat.activeSessionId}
            scopeKey={scopeKey}
            canCreate={chat.ready}
            offline={offline}
            onCreate={chat.newChat}
            onWorkspace={chat.chooseWorkspace}
            onConversation={openConversation}
            onMarkAllRead={
              offline
                ? undefined
                : () => {
                    void markAllRead();
                  }
            }
            onAllWorkspaces={offline ? undefined : () => setSheet("workspaces")}
            scheduledTasks={{
              count: activeTaskCount(schedules.tasks),
              running: schedules.tasks.some((task) => task.is_running),
            }}
            onScheduledTasks={offline ? undefined : () => setSheet("schedules")}
            pendingApps={pendingAppCount(apps.rows)}
            onApps={() => setSheet("apps")}
            onConversationAction={(row, action) => {
              if (action === "rename") chat.rename(row);
              else if (action === "read") void chat.markConversation(row);
              else chat.confirmDelete(row);
            }}
            profileTitle={profileTitle}
            profileInitials={user ? profileInitials(user) : "?"}
            onProfile={() => setSheet("profile")}
            error={drawerError}
            onDismissError={() => setDrawerError(null)}
          />
        }
      >
        <WorkspaceChatView
          chat={chat}
          scopeKey={scopeKey}
          composerWorkspace={newChatWorkspace}
          renderPublishedApp={renderPublishedApp}
          onOpenSettings={openSettings}
          onRetryConnection={() => {
            void retry();
          }}
          reconnecting={reconnecting}
          emptyText={
            offline
              ? conversations.length
                ? "Open a saved chat from the sidebar."
                : "No chats are saved on this device yet."
              : undefined
          }
          emptyContent={
            chat.needsSetup ? (
              <WorkspaceSetupGuide
                client={client}
                organizationId={session?.organizationId ?? null}
                isAdmin={isOrganizationAdmin(
                  organizations,
                  session?.organizationId,
                )}
                providerConnected={isModelProviderConnected(
                  chat.models,
                  chat.engines,
                )}
                refreshKey={settingsClosed}
                onOpenSettings={openSettings}
                onCreated={() => chat.loadOverview(true)}
              />
            ) : undefined
          }
          onPullUpAtEnd={toolsAvailable ? () => openSheet("tools") : undefined}
          pullUpLabel="Git, Desktop, Terminal & Files"
        />
      </Drawer>

      {workspace && (toolsAvailable || sheet === "tools") ? (
        <WorkspaceToolsSheet
          visible={sheet === "tools"}
          onClose={() => chat.closeSheet("tools")}
          client={client}
          workspaceId={workspace.id}
          workspaceName={workspace.name}
          media={chat.media}
          onInsertIntoChat={chat.insertIntoChat}
        />
      ) : null}

      <WorkspacesSheet
        visible={sheet === "workspaces"}
        onClose={() => chat.closeSheet("workspaces")}
        client={client}
        organizationId={session?.organizationId ?? null}
        workspaces={workspaces}
        loading={loading}
        error={chat.workspacesError}
        activeWorkspaceId={workspaceId}
        onOpenWorkspace={chat.chooseWorkspace}
        onRefresh={() => chat.loadOverview(true)}
      />

      <ScheduledTasksSheet
        visible={sheet === "schedules"}
        onClose={() => chat.closeSheet("schedules")}
        client={client}
        schedules={schedules}
        workspaces={workspaces}
        skills={chat.skills}
        catalog={agentCatalog}
        defaultWorkspaceId={workspaceId}
        onOpenChat={openRunChat}
      />

      <AppsSheet
        visible={sheet === "apps"}
        onClose={() => chat.closeSheet("apps")}
        apps={apps}
      />

      <ScheduledTaskSheet
        task={null}
        visible={sheet === "schedule-prompt"}
        client={client}
        workspaces={workspaces}
        skills={chat.skills}
        catalog={agentCatalog}
        prefill={{
          workspaceId: workspaceId ?? undefined,
          prompt: chat.draft.trim(),
          mode: chat.mode,
          harnessId: chat.harness,
          model: chat.model,
          effort: chat.effort,
          skillIds: chat.skillIds,
        }}
        onClose={() => chat.closeSheet("schedule-prompt")}
        onSaved={schedules.upsert}
        onDeleted={schedules.remove}
        onOpenChat={openRunChat}
      />

      <SettingsScreen
        visible={sheet === "profile"}
        initialPath={settingsPath}
        onClose={() => {
          setSettingsPath([]);
          setSettingsClosed((count) => count + 1);
          chat.closeSheet("profile");
        }}
        client={client}
        user={user}
        organizations={organizations}
        organizationId={session?.organizationId ?? null}
        serverUrl={session?.serverUrl ?? client.api.baseUrl}
        offline={offline}
        workspaces={workspaces}
        refreshWorkspaces={chat.refreshWorkspaces}
        onCatalogChanged={chat.reloadCatalogs}
        onOpenWorkspace={chat.chooseWorkspace}
        selectOrganization={(id) => {
          setSheet(null);
          void selectOrganization(id);
        }}
        createOrganization={async (name) => {
          await createOrganization(name);
          setSheet(null);
        }}
        signOut={() => {
          setSheet(null);
          void signOut();
        }}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  center: { flex: 1, alignItems: "center", justifyContent: "center" },
});
