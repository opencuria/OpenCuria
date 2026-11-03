/**
 * Actions on a scheduled task with the webapp's feedback: a native alert
 * before deleting, toasts that name the outcome afterwards.
 */
import { Alert } from "react-native";
import type {
  OpenCuriaClient,
  ScheduledTask,
  ScheduledTaskRun,
  Workspace,
} from "@opencuria/client";
import { showToast } from "../ui/toast";
import { errorText } from "../workspaces/workspaceOperations";
import { relativeRunTime } from "./scheduledTaskPresentation";

/**
 * Start a run outside the schedule; resolves to the run, or null on failure.
 * A stopped workspace starts first, which the server waits for.
 */
export async function runTaskNow(
  client: OpenCuriaClient,
  task: ScheduledTask,
  workspace?: Pick<Workspace, "name" | "status">,
): Promise<ScheduledTaskRun | null> {
  if (workspace && workspace.status !== "running")
    showToast({
      title: "Starting workspace",
      message: `${workspace.name} starts before the run begins. This can take a minute.`,
      tone: "info",
    });
  try {
    const run = await client.scheduledTasks.run(task.id);
    if (run.status === "running" || run.status === "claimed")
      showToast({
        title: run.status === "running" ? "Run started" : "Run queued",
        message:
          run.status === "running"
            ? `${task.name} is running in a new chat.`
            : `${task.name} starts as soon as its workspace is ready.`,
        tone: "success",
      });
    else
      showToast({
        title: "Run skipped",
        message:
          run.error ||
          (run.reason ? run.reason.replaceAll("_", " ") : "") ||
          "The task did not start.",
        tone: "warning",
      });
    return run;
  } catch (cause) {
    showToast({
      title: "Could not run task",
      message: errorText(cause, "Please try again."),
      tone: "error",
    });
    return null;
  }
}

/** Pause or resume the schedule; resolves to the saved task, or null. */
export async function setTaskEnabled(
  client: OpenCuriaClient,
  task: ScheduledTask,
  enabled: boolean,
): Promise<ScheduledTask | null> {
  try {
    const saved = await client.scheduledTasks.update(task.id, { enabled });
    const next = new Date(saved.next_run_at);
    showToast({
      title: enabled ? "Resumed" : "Paused",
      message: enabled
        ? Number.isNaN(next.getTime())
          ? undefined
          : `Next run ${relativeRunTime(next)}.`
        : `${saved.name} won’t run until you resume it.`,
      tone: enabled ? "success" : "info",
    });
    return saved;
  } catch (cause) {
    showToast({
      title: enabled ? "Could not resume task" : "Could not pause task",
      message: errorText(cause, "Please try again."),
      tone: "error",
    });
    return null;
  }
}

/**
 * Ask, then delete. `onAccepted` runs before the confirmation toast, e.g. to
 * close the sheet so the toast shows on the screen beneath.
 */
export function confirmDeleteTask(
  client: OpenCuriaClient,
  task: ScheduledTask,
  onAccepted?: () => void,
): Promise<boolean> {
  return new Promise((resolve) => {
    Alert.alert(
      `Delete “${task.name}”?`,
      "The schedule is removed. Chats from earlier runs stay.",
      [
        { text: "Cancel", style: "cancel", onPress: () => resolve(false) },
        {
          text: "Delete",
          style: "destructive",
          onPress: () => {
            void client.scheduledTasks.delete(task.id).then(
              () => {
                onAccepted?.();
                showToast({ title: "Scheduled task deleted", tone: "info" });
                resolve(true);
              },
              (cause: unknown) => {
                showToast({
                  title: "Could not delete task",
                  message: errorText(cause, "Please try again."),
                  tone: "error",
                });
                resolve(false);
              },
            );
          },
        },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}
