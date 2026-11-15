import React, { useEffect, useState } from "react";
import { Platform } from "react-native";
import type {
  OpenCuriaClient,
  ScheduledTask,
  Skill,
  Workspace,
} from "@opencuria/client";
import { NativeForm } from "../ui/NativeForm";
import type {
  NativeFormItemRow,
  NativeFormSection,
  NativeFormSwipeAction,
} from "../ui/NativeForm.types";
import type { NativeMenuAction } from "../ui/NativeMenu";
import { NativeModal } from "../ui/primitives";
import { useTheme } from "../ui/theme";
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
  taskSubtitle,
  taskTile,
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
  const { theme } = useTheme();
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
    const tile = taskTile(state, theme);
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
      ...(state === "scheduled"
        ? leadingClock(new Date(task.next_run_at))
        : { symbol: tile.symbol, tint: tile.tint }),
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

  const sections: NativeFormSection[] =
    !schedules.tasks.length && schedules.error
      ? [
          {
            id: "error",
            rows: [
              {
                kind: "note",
                id: "schedules-error",
                text: `Could not load scheduled tasks: ${schedules.error}`,
                tone: "danger",
              },
              {
                kind: "button",
                id: "schedules-retry",
                title: "Retry",
                symbol: "arrow.counterclockwise",
                busy: schedules.loading,
                onPress: () => void schedules.refresh(),
              },
            ],
          },
        ]
      : !schedules.loaded
        ? [
            {
              id: "loading",
              rows: [
                {
                  kind: "note",
                  id: "schedules-loading",
                  text: "Loading scheduled tasks…",
                },
              ],
            },
          ]
        : scheduleAgenda(schedules.tasks, { now, showEnded }).map((group) => ({
            id: group.id,
            title: group.title,
            rows: group.tasks.map(row),
          }));

  return (
    <NativeModal
      visible
      onClose={onClose}
      title="Scheduled tasks"
      scrollable={false}
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
      testID="scheduled-tasks-sheet"
    >
      <NativeForm
        sections={sections}
        onRefresh={schedules.refresh}
        empty={
          hasEnded && !showEnded
            ? {
                title: "Nothing scheduled",
                description: "Ended tasks are hidden. Show them from the menu.",
                symbol: "calendar.badge.clock",
              }
            : {
                title: "No scheduled tasks",
                description:
                  "Let an agent run a prompt on its own, once or on repeat. Each run starts a new chat.",
                symbol: "calendar.badge.clock",
              }
        }
        contentInsetBottom={NEW_TASK_INSET}
        testID="scheduled-task-list"
      />
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
        onSaved={schedules.upsert}
        onDeleted={schedules.remove}
        onOpenChat={onOpenChat}
      />
    </NativeModal>
  );
}
