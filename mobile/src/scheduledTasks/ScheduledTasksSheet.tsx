import React, { useEffect, useState } from "react";
import { Platform } from "react-native";
import type {
  OpenCuriaClient,
  ScheduledTask,
  Skill,
  Workspace,
} from "@opencuria/client";
import { errorRows, loadErrorState } from "../ui/formStates";
import type {
  NativeFormEmptyState,
  NativeFormItemRow,
  NativeFormSection,
  NativeFormSwipeAction,
} from "../ui/NativeForm.types";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { NativeModal } from "../ui/primitives";
import { ScheduledTaskSheet } from "./ScheduledTaskSheet";
import { deviceTimeZone, type AgentCatalog } from "./scheduledTaskForm";
import {
  confirmDeleteTask,
  runTaskNow,
  setTaskEnabled,
} from "./scheduledTaskOperations";
import {
  clockParts,
  scheduleAgenda,
  taskState,
  taskStatus,
  taskSubtitle,
  taskSymbol,
} from "./scheduledTaskPresentation";
import { useNow, type ScheduledTasksState } from "./useScheduledTasks";

function leadingClock(date: Date): {
  leadingText: string;
  leadingCaption?: string;
} {
  const { time, period } = clockParts(date);
  return period
    ? { leadingText: time, leadingCaption: period }
    : { leadingText: time };
}

/** Room for Android's "New scheduled task" button; iOS shows it in the navigation bar. */
const NEW_TASK_INSET = Platform.OS === "android" ? 88 : 0;

export interface ScheduledTasksSheetProps {
  visible: boolean;
  onClose: () => void;
  client: OpenCuriaClient;
  schedules: ScheduledTasksState;
  workspaces: Workspace[];
  skills: Skill[];
  catalog: AgentCatalog;
  /** Workspace a new task starts in, e.g. the open chat's. */
  defaultWorkspaceId: string | null;
  /** Open a run's chat; the caller closes the sheets. */
  onOpenChat: (workspaceId: string, sessionId: string) => void;
}

/**
 * The agenda of scheduled tasks: what runs now, then each day's runs led by
 * their time, then paused tasks. Rows open the task; swipes and menus run,
 * pause, resume or delete it.
 */
export function ScheduledTasksSheet({
  visible,
  onClose,
  client,
  schedules,
  workspaces,
  skills,
  catalog,
  defaultWorkspaceId,
  onOpenChat,
}: ScheduledTasksSheetProps): React.JSX.Element | null {
  const now = useNow();
  const [detailId, setDetailId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [showEnded, setShowEnded] = useState(false);
  // Closing the list (e.g. to open a chat) also closes the sheets above it.
  useEffect(() => {
    if (visible) return;
    setDetailId(null);
    setCreating(false);
  }, [visible]);
  if (!visible) return null;

  const detail = schedules.tasks.find((task) => task.id === detailId) ?? null;
  const names = new Map(workspaces.map((entry) => [entry.id, entry.name]));
  const zone = deviceTimeZone();
  const hasEnded = schedules.tasks.some(
    (task) => taskState(task, now) === "ended",
  );

  const act = (task: ScheduledTask, id: string) => {
    if (id === "run")
      void runTaskNow(
        client,
        task,
        workspaces.find((entry) => entry.id === task.workspace_id),
      ).then((run) => {
        if (run) void schedules.refresh();
      });
    else if (id === "pause" || id === "resume")
      void setTaskEnabled(client, task, id === "resume").then((saved) => {
        if (saved) schedules.upsert(saved);
      });
    else if (id === "delete")
      void confirmDeleteTask(client, task).then((deleted) => {
        if (deleted) schedules.remove(task.id);
      });
  };

  const row = (task: ScheduledTask): NativeFormItemRow => {
    const state = taskState(task, now);
    const toggle: NativeFormSwipeAction[] =
      state === "ended"
        ? []
        : task.enabled
          ? [
              {
                id: "pause",
                title: "Pause",
                symbol: "pause.fill",
                tone: "muted",
              },
            ]
          : [
              {
                id: "resume",
                title: "Resume",
                symbol: "play.circle",
                tone: "accent",
              },
            ];
    const actions: NativeMenuAction[] = [
      { id: "run", title: "Run now", symbol: "play.fill" },
      ...toggle,
      { id: "delete", title: "Delete", symbol: "trash", destructive: true },
    ];
    return {
      kind: "item",
      id: task.id,
      testID: `scheduled-task-${task.id}`,
      title: task.name,
      subtitle: taskSubtitle(task, names.get(task.workspace_id), zone),
      status: taskStatus(state),
      ...(state === "scheduled"
        ? leadingClock(new Date(task.next_run_at))
        : taskSymbol(state)),
      busy: state === "running",
      onPress: () => setDetailId(task.id),
      actions,
      leadingSwipe: [
        { id: "run", title: "Run now", symbol: "play.fill", tone: "success" },
      ],
      trailingSwipe: [
        { id: "delete", title: "Delete", symbol: "trash", destructive: true },
        ...toggle,
      ],
      onAction: (id) => act(task, id),
    };
  };

  // Nothing to show yet: a failure replaces the spinner. A task created
  // meanwhile (its response supersedes the first load) shows right away.
  const failed = !schedules.tasks.length && Boolean(schedules.error);
  const waiting = !schedules.loaded && !schedules.tasks.length && !failed;
  const agenda: NativeFormSection[] = scheduleAgenda(schedules.tasks, {
    now,
    showEnded,
  }).map((group) => ({
    id: group.id,
    title: group.title,
    rows: group.tasks.map(row),
  }));
  const sections: NativeFormSection[] =
    failed || waiting
      ? []
      : schedules.error
        ? [
            {
              id: "error",
              rows: errorRows(
                "scheduled-tasks",
                schedules.error,
                () => void schedules.refresh(),
                schedules.loading,
              ),
            },
            ...agenda,
          ]
        : agenda;
  const empty: NativeFormEmptyState = failed
    ? loadErrorState(
        "scheduled tasks",
        schedules.error ?? "",
        () => void schedules.refresh(),
        schedules.loading,
      )
    : hasEnded && !showEnded
      ? {
          title: "Nothing scheduled",
          description: "All tasks have ended.",
          symbol: "calendar.badge.clock",
          action: {
            title: "Show ended tasks",
            onPress: () => setShowEnded(true),
            testID: "scheduled-tasks-show-ended",
          },
        }
      : {
          title: "No scheduled tasks yet",
          description:
            "Let an agent run a prompt on its own, once or on repeat. Each run starts a new chat.",
          symbol: "calendar.badge.clock",
          action: {
            title: "New scheduled task",
            symbol: "plus",
            onPress: () => setCreating(true),
            testID: "scheduled-tasks-empty-new",
          },
        };
  const created = (task: ScheduledTask) => {
    schedules.upsert(task);
    // The new task supersedes a first load still in flight or failed.
    if (!schedules.loaded) void schedules.refresh();
  };

  return (
    <NativeModal
      visible
      onClose={onClose}
      title="Scheduled tasks"
      largeTitle
      primaryAction={{
        title: "New scheduled task",
        symbol: "plus",
        onPress: () => setCreating(true),
        testID: "new-scheduled-task",
      }}
      actions={
        hasEnded
          ? [{ id: "ended", title: "Show ended tasks", selected: showEnded }]
          : undefined
      }
      onAction={(id) => {
        if (id === "ended") setShowEnded((value) => !value);
      }}
      form={{
        sections,
        onRefresh: schedules.refresh,
        empty,
        loading: waiting,
        contentInsetBottom: NEW_TASK_INSET,
        testID: "scheduled-task-list",
      }}
      testID="scheduled-tasks-sheet"
    >
      <ScheduledTaskSheet
        task={detail}
        visible={Boolean(detail)}
        client={client}
        workspaces={workspaces}
        skills={skills}
        catalog={catalog}
        onClose={() => setDetailId(null)}
        onSaved={schedules.upsert}
        onDeleted={schedules.remove}
        onOpenChat={(workspaceId, sessionId) => {
          setDetailId(null);
          onOpenChat(workspaceId, sessionId);
        }}
      />
      <ScheduledTaskSheet
        task={null}
        visible={creating}
        client={client}
        workspaces={workspaces}
        skills={skills}
        catalog={catalog}
        prefill={
          defaultWorkspaceId ? { workspaceId: defaultWorkspaceId } : undefined
        }
        onClose={() => setCreating(false)}
        onSaved={created}
        onDeleted={schedules.remove}
        onOpenChat={onOpenChat}
      />
    </NativeModal>
  );
}
