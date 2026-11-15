import React, { useCallback, useEffect, useRef, useState } from "react";
import { Alert } from "react-native";
import {
  isScheduleRunActive,
  scheduleRunWorkspaceId,
} from "@opencuria/chat-core/scheduledTasks";
import type {
  OpenCuriaClient,
  ScheduledTask,
  ScheduledTaskRun,
  Skill,
  Workspace,
} from "@opencuria/client";
import { ModelSheet, modelSheetEngines } from "../components/ModelSheet";
import { NativeForm } from "../ui/NativeForm";
import type {
  NativeFormQuickAction,
  NativeFormRow,
  NativeFormSection,
} from "../ui/NativeForm.types";
import { NativeModal } from "../ui/primitives";
import { useTheme } from "../ui/theme";
import { tileColor } from "../ui/tileColors";
import { showToast } from "../ui/toast";
import { errorText } from "../workspaces/workspaceOperations";
import {
  agentSummary,
  blankTaskForm,
  catalogModels,
  createTaskPayload,
  taskFormEnabled,
  taskFormErrors,
  taskFormFromTask,
  taskFormPatch,
  withAgent,
  withModel,
  type AgentCatalog,
  type TaskForm,
  type TaskPrefill,
} from "./scheduledTaskForm";
import { scheduleFormSection } from "./scheduleFormSection";
import {
  confirmDeleteTask,
  runTaskNow,
  setTaskEnabled,
} from "./scheduledTaskOperations";
import {
  latestSettledRun,
  relativeRunTime,
  runPresentation,
  taskState,
  taskStatusLine,
  taskTile,
} from "./scheduledTaskPresentation";
import { useNow } from "./useScheduledTasks";

/** Recent runs shown before "Show all runs". */
const VISIBLE_RUNS = 5;
/** Matches the webapp's run-history polling while a run is active. */
const ACTIVE_RUN_POLL_MS = 10_000;
const HIDDEN_WORKSPACES = new Set(["deleted", "deleting", "pending_deletion"]);

export interface ScheduledTaskSheetProps {
  /** The task to show and edit; `null` creates a new one. */
  task: ScheduledTask | null;
  visible: boolean;
  client: OpenCuriaClient;
  workspaces: Workspace[];
  skills: Skill[];
  catalog: AgentCatalog;
  /** Initial values of a new task. */
  prefill?: TaskPrefill;
  onClose: () => void;
  /** The server saved the task (created, edited, paused or resumed). */
  onSaved: (task: ScheduledTask) => void;
  onDeleted: (taskId: string) => void;
  onOpenChat: (workspaceId: string, sessionId: string) => void;
}

/**
 * One scheduled task, like an iOS settings page: status and quick actions
 * on top, then what runs (name, workspace, prompt, model), when it runs, its
 * recent runs and deletion. Without a task it creates one.
 */
export function ScheduledTaskSheet(
  props: ScheduledTaskSheetProps,
): React.JSX.Element | null {
  if (!props.visible) return null;
  return <TaskSheet key={props.task?.id ?? "new"} {...props} />;
}

interface RunsState {
  runs: ScheduledTaskRun[];
  loading: boolean;
  error: string | null;
}

/** Run history of a saved task, polled while a run is queued or running. */
function useTaskRuns(
  client: OpenCuriaClient,
  task: ScheduledTask | null,
): RunsState & { reload: () => Promise<void> } {
  const taskId = task?.id ?? null;
  const [state, setState] = useState<RunsState>({
    runs: [],
    loading: Boolean(taskId),
    error: null,
  });
  const generation = useRef(0);
  const reload = useCallback(async () => {
    if (!taskId) return;
    const request = ++generation.current;
    setState((current) => ({ ...current, loading: true }));
    try {
      const runs = await client.scheduledTasks.runs(taskId);
      if (request === generation.current)
        setState({ runs, loading: false, error: null });
    } catch (cause) {
      if (request === generation.current)
        setState((current) => ({
          ...current,
          loading: false,
          error: errorText(cause, "Could not load runs."),
        }));
    }
  }, [client, taskId]);

  // Follow the task's own changes, e.g. a dispatch starting or settling.
  const runKey = `${task?.is_running}:${task?.next_run_at}`;
  useEffect(() => {
    void reload();
  }, [reload, runKey]);
  const active = state.runs.some(isScheduleRunActive);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void reload(), ACTIVE_RUN_POLL_MS);
    return () => clearInterval(timer);
  }, [active, reload]);
  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );
  return { ...state, reload };
}

function TaskSheet({
  task,
  client,
  workspaces,
  skills,
  catalog,
  prefill,
  onClose,
  onSaved,
  onDeleted,
  onOpenChat,
}: ScheduledTaskSheetProps): React.JSX.Element {
  const { theme } = useTheme();
  const now = useNow();
  const available = workspaces.filter(
    (workspace) => !HIDDEN_WORKSPACES.has(workspace.status),
  );
  const [baseline] = useState<TaskForm>(() =>
    task
      ? taskFormFromTask(task)
      : blankTaskForm(catalog, {
          ...prefill,
          workspaceId:
            prefill?.workspaceId ??
            (available.length === 1 ? available[0]!.id : ""),
        }),
  );
  const [form, setForm] = useState<TaskForm>(baseline);
  const [touched, setTouched] = useState(false);
  /** A model the user (or the saved task) picked; mode changes keep it. */
  const [modelChosen, setModelChosen] = useState(
    Boolean(task || prefill?.model),
  );
  const [saving, setSaving] = useState(false);
  const [acting, setActing] = useState(false);
  const [modelSheet, setModelSheet] = useState(false);
  const [allRuns, setAllRuns] = useState(false);
  const runs = useTaskRuns(client, task);

  const edit = (next: TaskForm) => {
    setTouched(true);
    setForm(next);
  };

  // Follow server changes until edited, as the workspace detail does.
  const serverKey = task ? JSON.stringify(taskFormFromTask(task)) : "";
  useEffect(() => {
    if (task && !touched) setForm(JSON.parse(serverKey) as TaskForm);
  }, [serverKey, touched]);

  // A new task follows the agent defaults once the catalogs arrive.
  const models = catalogModels(catalog, form.harnessId);
  const defaultsKey = `${models.length}:${catalog.agentConfigs.length}:${catalog.engines.length}`;
  useEffect(() => {
    if (task || modelChosen) return;
    setForm((current) =>
      withAgent(current, current.harnessId, current.mode, catalog, false),
    );
  }, [defaultsKey]);

  const errors = taskFormErrors(task, form, now);
  const valid = Object.keys(errors).length === 0;
  const patch = task ? taskFormPatch(task, form, now) : null;
  const dirty = task
    ? Object.keys(patch!).length > 0
    : JSON.stringify({ ...form, model: "", effort: "" }) !==
      JSON.stringify({ ...baseline, model: "", effort: "" });
  const busy = saving || acting;

  const save = async () => {
    if (!valid || busy || (task && !dirty)) return;
    setSaving(true);
    try {
      const saved = task
        ? await client.scheduledTasks.update(task.id, patch!)
        : await client.scheduledTasks.create(createTaskPayload(form));
      onSaved(saved);
      // Close first so the confirmation shows on the screen beneath.
      onClose();
      const next = new Date(saved.next_run_at);
      showToast({
        title: task ? "Saved" : "Scheduled",
        message:
          saved.enabled && !Number.isNaN(next.getTime())
            ? `Next run ${relativeRunTime(next)}.`
            : undefined,
        tone: "success",
      });
    } catch (cause) {
      showToast({
        title: task ? "Could not save task" : "Could not schedule task",
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
      task
        ? "Your changes to this scheduled task are not saved."
        : "This scheduled task is not created yet.",
      [
        { text: "Keep editing", style: "cancel" },
        { text: "Discard", style: "destructive", onPress: onClose },
      ],
    );
  };

  const act = async (action: () => Promise<unknown>) => {
    setActing(true);
    try {
      await action();
    } finally {
      setActing(false);
    }
  };

  const workspace = available.find((entry) => entry.id === form.workspaceId);
  const workspaceRows: NativeFormRow[] = available.length
    ? [
        {
          kind: "picker",
          id: "task-workspace",
          label: "Workspace",
          value: form.workspaceId,
          groups: [
            {
              options: [
                ...(form.workspaceId && !workspace
                  ? [
                      {
                        value: form.workspaceId,
                        label: "Unavailable workspace",
                        disabled: true,
                      },
                    ]
                  : []),
                ...(form.workspaceId ? [] : [{ value: "", label: "Choose" }]),
                ...available.map((entry) => ({
                  value: entry.id,
                  label: entry.name,
                })),
              ],
            },
          ],
          onChange: (workspaceId) => edit({ ...form, workspaceId }),
          disabled: busy,
        },
      ]
    : [
        {
          kind: "note",
          id: "task-no-workspace",
          text: "Create a workspace before scheduling a task.",
          tone: "danger",
        },
      ];

  const knownSkills = new Set(skills.map((skill) => skill.id));
  const toggleSkill = (id: string) =>
    edit({
      ...form,
      skillIds: form.skillIds.includes(id)
        ? form.skillIds.filter((entry) => entry !== id)
        : [...form.skillIds, id],
    });
  const skillRows: NativeFormRow[] = [
    ...skills.map(
      (skill): NativeFormRow => ({
        kind: "toggle",
        id: `task-skill-${skill.id}`,
        label: skill.name,
        value: form.skillIds.includes(skill.id),
        onChange: () => toggleSkill(skill.id),
        disabled: busy,
      }),
    ),
    ...form.skillIds
      .filter((id) => !knownSkills.has(id))
      .map(
        (id): NativeFormRow => ({
          kind: "toggle",
          id: `task-skill-${id}`,
          label: "Unavailable skill",
          subtitle: "Turn off to save",
          subtitleTone: "danger",
          value: true,
          onChange: () => toggleSkill(id),
          disabled: busy,
        }),
      ),
  ];

  const state = task ? taskState(task, now) : "scheduled";
  const enabledAfterSave = taskFormEnabled(task, form, now);
  const sections: NativeFormSection[] = [
    ...(task ? [headerSection()] : []),
    {
      id: "task",
      rows: [
        {
          kind: "text",
          id: "task-name",
          label: "Name",
          placeholder: "Name, e.g. Weekly dependency review",
          value: form.name,
          prose: true,
          onChange: (name) => edit({ ...form, name }),
          disabled: busy,
        },
        ...workspaceRows,
      ],
      footer:
        workspace?.status === "stopped"
          ? "The stopped workspace starts automatically when a run begins."
          : undefined,
    },
    {
      id: "prompt",
      title: "Prompt",
      rows: [
        {
          kind: "text",
          id: "task-prompt",
          label: "Prompt",
          placeholder: "What should the agent do each time?",
          value: form.prompt,
          multiline: true,
          lines: [3, 12],
          prose: true,
          onChange: (prompt) => edit({ ...form, prompt }),
          disabled: busy,
        },
        {
          kind: "value",
          id: "task-model",
          label: "Model",
          value: agentSummary(form, catalog),
          onPress: () => setModelSheet(true),
          disabled: busy,
        },
      ],
      footer: "Each run starts a new chat with this prompt.",
    },
    scheduleFormSection({
      schedule: form.schedule,
      timezone: form.timezone,
      paused: !enabledAfterSave,
      nextRunAt: task && !dirty && task.enabled ? task.next_run_at : null,
      error: errors.schedule,
      onSchedule: (schedule) => edit({ ...form, schedule }),
      onTimezone: (timezone) => edit({ ...form, timezone }),
      disabled: busy,
      now,
    }),
    ...(skillRows.length
      ? [{ id: "skills", title: "Skills", rows: skillRows }]
      : []),
    ...(task ? [runsSection(task), deleteSection(task)] : []),
  ];

  function headerSection(): NativeFormSection {
    const current = task!;
    const tile = taskTile(state, theme);
    const status = taskStatusLine(current, now);
    const last = latestSettledRun(runs.runs);
    const failed = last?.status === "error";
    const chat = runs.runs.find((run) => run.session_id);
    const ended = state === "ended";
    const actions: NativeFormQuickAction[] = [
      {
        id: "run",
        title: "Run now",
        symbol: "play.fill",
        disabled: dirty || busy,
      },
      ...(ended
        ? []
        : [
            current.enabled
              ? {
                  id: "pause",
                  title: "Pause",
                  symbol: "pause.fill" as const,
                  disabled: dirty || busy,
                }
              : {
                  id: "resume",
                  title: "Resume",
                  symbol: "play.circle" as const,
                  disabled: dirty || busy,
                },
          ]),
      {
        id: "chat",
        title: "Last chat",
        symbol: "bubble.left.fill",
        disabled: !chat,
      },
    ];
    return {
      id: "header",
      rows: [
        {
          kind: "header",
          id: "task-header",
          title: current.name,
          subtitle: failed ? `Last run failed · ${status.text}` : status.text,
          subtitleTone: failed ? "danger" : status.tone,
          symbol: tile.symbol,
          tint:
            failed && state !== "running" ? tileColor("red", theme) : tile.tint,
          busy: state === "running",
          actions,
          onAction: (id) => {
            if (id === "run")
              void act(async () => {
                if (
                  await runTaskNow(
                    client,
                    current,
                    workspaces.find(
                      (entry) => entry.id === current.workspace_id,
                    ),
                  )
                )
                  await runs.reload();
              });
            else if (id === "pause" || id === "resume")
              void act(async () => {
                const saved = await setTaskEnabled(
                  client,
                  current,
                  id === "resume",
                );
                if (saved) onSaved(saved);
              });
            else if (id === "chat" && chat?.session_id)
              onOpenChat(
                scheduleRunWorkspaceId(chat, current),
                chat.session_id,
              );
          },
        },
      ],
    };
  }

  function runsSection(current: ScheduledTask): NativeFormSection {
    const shown = allRuns ? runs.runs : runs.runs.slice(0, VISIBLE_RUNS);
    const rows: NativeFormRow[] =
      !runs.runs.length && runs.loading
        ? [{ kind: "note", id: "runs-loading", text: "Loading runs…" }]
        : !runs.runs.length && runs.error
          ? [
              {
                kind: "note",
                id: "runs-error",
                text: runs.error,
                tone: "danger",
              },
              {
                kind: "button",
                id: "runs-retry",
                title: "Retry",
                symbol: "arrow.counterclockwise",
                busy: runs.loading,
                onPress: () => void runs.reload(),
              },
            ]
          : !runs.runs.length
            ? [
                {
                  kind: "note",
                  id: "runs-empty",
                  text: "No runs yet. They appear here, each with its own chat.",
                },
              ]
            : [
                ...shown.map((run): NativeFormRow => {
                  const view = runPresentation(run, theme, now);
                  const sessionId = run.session_id;
                  return {
                    kind: "item",
                    id: `run-${run.id}`,
                    title: view.title,
                    subtitle: view.subtitle,
                    subtitleTone: view.subtitleTone,
                    symbol: view.symbol,
                    tint: view.tint,
                    busy: view.busy,
                    onPress: sessionId
                      ? () =>
                          onOpenChat(
                            scheduleRunWorkspaceId(run, current),
                            sessionId,
                          )
                      : undefined,
                  };
                }),
                ...(runs.runs.length > shown.length
                  ? [
                      {
                        kind: "button" as const,
                        id: "runs-all",
                        title: `Show all ${runs.runs.length} runs`,
                        onPress: () => setAllRuns(true),
                      },
                    ]
                  : []),
              ];
    return { id: "runs", title: "Runs", rows };
  }

  function deleteSection(current: ScheduledTask): NativeFormSection {
    return {
      id: "danger",
      rows: [
        {
          kind: "button",
          id: "task-delete",
          title: "Delete scheduled task",
          symbol: "trash",
          destructive: true,
          disabled: busy,
          onPress: () => {
            void confirmDeleteTask(client, current, () => {
              onDeleted(current.id);
              onClose();
            });
          },
        },
      ],
      footer: `Created ${new Date(current.created_at).toLocaleDateString()}`,
    };
  }

  const engineName =
    catalog.engines.find((engine) => engine.id === form.harnessId)?.name ??
    form.harnessId;
  return (
    <NativeModal
      visible
      onClose={close}
      title={task ? "Scheduled task" : "New scheduled task"}
      scrollable={false}
      swipeToDismiss={!dirty}
      primaryAction={{
        title: task ? "Save" : "Schedule",
        onPress: () => {
          void save();
        },
        disabled: !valid || busy || Boolean(task && !dirty),
        busy: saving,
        testID: "scheduled-task-save",
      }}
      testID="scheduled-task-sheet"
    >
      <NativeForm sections={sections} />
      <ModelSheet
        visible={modelSheet}
        onClose={() => setModelSheet(false)}
        models={models}
        recentModelIds={catalog.recentModelIds}
        model={form.model}
        effort={form.effort}
        engines={modelSheetEngines(catalog.engines)}
        engine={form.harnessId}
        mode={form.mode}
        unavailableMessage={
          catalog.error ??
          `${engineName} models unavailable. Check the provider account in Settings.`
        }
        onModelChange={(model) => {
          setModelChosen(true);
          edit(withModel(form, model, catalog));
        }}
        onEffortChange={(effort) => {
          setModelChosen(true);
          edit({ ...form, effort });
        }}
        onAgentChange={(engine, mode) => {
          const switched = engine !== form.harnessId;
          if (switched) setModelChosen(false);
          edit(
            withAgent(form, engine, mode, catalog, modelChosen && !switched),
          );
        }}
      />
    </NativeModal>
  );
}
