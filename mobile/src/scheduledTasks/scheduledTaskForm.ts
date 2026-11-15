/**
 * Editor state of a scheduled task: what the form holds, how it validates
 * and which fields saving sends. Schedule rules come from chat-core.
 */
import type {
  HarnessId,
  HarnessSessionMode,
} from "@opencuria/chat-core/harness";
import {
  formatEffort,
  harnessAgentDefaults,
  harnessEngineLabel,
  resolveCatalogModel,
  snapEffort,
  type HarnessEngine,
  type ProviderModel,
} from "@opencuria/chat-core/harnessModels";
import {
  COMMON_TIME_ZONES,
  defaultScheduleDraft,
  isScheduleEnded,
  isValidTimeZone,
  onceScheduleError,
  scheduleDraftFromTask,
  schedulePayload,
  validateSchedule,
  validateScheduleDraft,
  type ScheduleDraft,
  type ScheduledTask,
  type ScheduledTaskInput,
  type ScheduledTaskPatch,
} from "@opencuria/chat-core/scheduledTasks";

/** Engines, model catalogs and agent defaults shared with the chat composer. */
export interface AgentCatalog {
  engines: HarnessEngine[];
  /** Native models. */
  models: ProviderModel[];
  /** Catalogs of the external engines by id. */
  engineModels: Record<string, ProviderModel[]>;
  agentConfigs: Array<{ agent: string; model: string; effort: string }>;
  /** Recently used model ids, newest first. */
  recentModelIds: string[];
  error: string | null;
}

export function catalogModels(
  catalog: AgentCatalog,
  engine: HarnessId,
): ProviderModel[] {
  return engine === "native"
    ? catalog.models
    : (catalog.engineModels[engine] ?? []);
}

export interface TaskForm {
  name: string;
  workspaceId: string;
  prompt: string;
  mode: HarnessSessionMode;
  harnessId: HarnessId;
  model: string;
  effort: string;
  skillIds: string[];
  schedule: ScheduleDraft;
  timezone: string;
}

/** Initial values of a new task, e.g. the chat it is scheduled from. */
export interface TaskPrefill {
  workspaceId?: string;
  prompt?: string;
  mode?: HarnessSessionMode;
  harnessId?: HarnessId;
  model?: string;
  effort?: string;
  skillIds?: string[];
}

export function deviceTimeZone(): string {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return zone && isValidTimeZone(zone) ? zone : "UTC";
  } catch {
    return "UTC";
  }
}

/**
 * A new task runs once at the next 09:00 in the device's zone, with the
 * prefilled agent or the engine's defaults.
 */
export function blankTaskForm(
  catalog: AgentCatalog,
  prefill: TaskPrefill = {},
  timezone = deviceTimeZone(),
): TaskForm {
  const harnessId = prefill.harnessId ?? "native";
  const mode = prefill.mode ?? "build";
  const agent = prefill.model
    ? { model: prefill.model, effort: prefill.effort ?? "" }
    : agentDefaults(catalog, harnessId, mode);
  return {
    name: "",
    workspaceId: prefill.workspaceId ?? "",
    prompt: prefill.prompt ?? "",
    mode,
    harnessId,
    model: agent.model,
    effort: agent.effort,
    skillIds: [...(prefill.skillIds ?? [])],
    schedule: defaultScheduleDraft(timezone),
    timezone,
  };
}

export function taskFormFromTask(task: ScheduledTask): TaskForm {
  return {
    name: task.name,
    workspaceId: task.workspace_id,
    prompt: task.prompt,
    mode: task.mode,
    harnessId: task.harness_id ?? "native",
    model: task.model,
    effort: task.reasoning_effort,
    skillIds: [...task.skill_ids],
    schedule: scheduleDraftFromTask(task),
    timezone: task.timezone_name,
  };
}

function agentDefaults(
  catalog: AgentCatalog,
  engine: HarnessId,
  mode: HarnessSessionMode,
): { model: string; effort: string } {
  return harnessAgentDefaults(engine, mode, {
    engines: catalog.engines,
    agentConfigs: catalog.agentConfigs,
    models: catalogModels(catalog, engine),
  });
}

/**
 * Change the engine or mode. Another engine starts from its defaults; a new
 * mode keeps a chosen model and otherwise follows the mode's defaults.
 */
export function withAgent(
  form: TaskForm,
  engine: HarnessId,
  mode: HarnessSessionMode,
  catalog: AgentCatalog,
  keepModel: boolean,
): TaskForm {
  if (engine === form.harnessId && keepModel) return { ...form, mode };
  return {
    ...form,
    harnessId: engine,
    mode,
    ...agentDefaults(catalog, engine, mode),
  };
}

/** Choose a model, keeping the effort when the model supports it. */
export function withModel(
  form: TaskForm,
  model: string,
  catalog: AgentCatalog,
): TaskForm {
  const entry = resolveCatalogModel(
    catalogModels(catalog, form.harnessId),
    model,
  );
  return { ...form, model, effort: snapEffort(entry, form.effort) };
}

/** "Sonnet 4.5 High", with the engine and Plan mode in front when set. */
export function agentSummary(form: TaskForm, catalog: AgentCatalog): string {
  const model = resolveCatalogModel(
    catalogModels(catalog, form.harnessId),
    form.model,
  );
  const name = model?.name ?? (form.model || "Default model");
  return [
    form.harnessId === "native"
      ? ""
      : harnessEngineLabel(form.harnessId, catalog.engines),
    form.mode === "plan" ? "Plan" : "",
    form.effort ? `${name} ${formatEffort(form.effort)}` : name,
  ]
    .filter(Boolean)
    .join(" · ");
}

const SCHEDULE_KEYS = [
  "recurrence",
  "weekdays",
  "local_time",
  "cron_expression",
  "run_date",
  "timezone_name",
] as const;

function fields(form: TaskForm, enabled: boolean): ScheduledTaskInput {
  return {
    name: form.name.trim(),
    workspace_id: form.workspaceId,
    prompt: form.prompt.trim(),
    mode: form.mode,
    harness_id: form.harnessId,
    model: form.model,
    reasoning_effort: form.effort,
    skill_ids: form.skillIds,
    ...schedulePayload(form.schedule),
    timezone_name: form.timezone,
    enabled,
  };
}

function scheduleChanged(task: ScheduledTask, form: TaskForm): boolean {
  const next = fields(form, task.enabled);
  const saved = fields(taskFormFromTask(task), task.enabled);
  return SCHEDULE_KEYS.some(
    (key) => JSON.stringify(next[key]) !== JSON.stringify(saved[key]),
  );
}

/**
 * New tasks are enabled; a paused task stays paused. An ended one-time task
 * is re-armed when its schedule changes, since that is the only way to run
 * it again.
 */
export function taskFormEnabled(
  task: ScheduledTask | null,
  form: TaskForm,
  now = new Date(),
): boolean {
  if (!task || task.enabled) return true;
  return isScheduleEnded(task, now) && scheduleChanged(task, form);
}

export function createTaskPayload(form: TaskForm): ScheduledTaskInput {
  return fields(form, true);
}

/**
 * Only the fields that changed. Schedule fields travel together so that the
 * server validates one complete cadence.
 */
export function taskFormPatch(
  task: ScheduledTask,
  form: TaskForm,
  now = new Date(),
): ScheduledTaskPatch {
  const next = fields(form, taskFormEnabled(task, form, now));
  const saved = fields(taskFormFromTask(task), task.enabled);
  const patch: Record<string, unknown> = {};
  for (const key of Object.keys(next) as Array<keyof ScheduledTaskInput>) {
    if (JSON.stringify(next[key]) !== JSON.stringify(saved[key]))
      patch[key] = next[key];
  }
  if (SCHEDULE_KEYS.some((key) => key in patch))
    for (const key of SCHEDULE_KEYS) patch[key] = next[key];
  return patch as ScheduledTaskPatch;
}

export interface TaskFormErrors {
  name?: string;
  workspace?: string;
  prompt?: string;
  /** The first problem of the schedule, shown below it. */
  schedule?: string;
}

/**
 * Problems that block saving. A one-time run must lie ahead only when it is
 * new, moved or re-armed, so a passed run does not block renaming a task.
 */
export function taskFormErrors(
  task: ScheduledTask | null,
  form: TaskForm,
  now = new Date(),
): TaskFormErrors {
  const payload = schedulePayload(form.schedule);
  const base = validateSchedule({
    name: form.name,
    workspace_id: form.workspaceId,
    prompt: form.prompt,
    recurrence: payload.recurrence,
    weekdays: payload.weekdays,
    timezone_name: form.timezone,
  }).errors;
  const draft = validateScheduleDraft(form.schedule);
  const enabled = taskFormEnabled(task, form, now);
  const timing =
    !task || scheduleChanged(task, form) || (enabled && !task.enabled)
      ? onceScheduleError(form.schedule, form.timezone, enabled, now)
      : "";
  const schedule =
    draft.date ||
    draft.weekdays ||
    draft.times ||
    draft.window ||
    draft.cron_expression ||
    base.timezone_name ||
    timing;
  const errors: TaskFormErrors = {};
  if (base.name) errors.name = base.name;
  if (base.workspace_id) errors.workspace = base.workspace_id;
  if (base.prompt) errors.prompt = base.prompt;
  if (schedule) errors.schedule = schedule;
  return errors;
}

export interface TimeZoneGroup {
  title: string;
  zones: string[];
}

/** The device's zone first, then common zones and the task's own zone. */
export function timeZoneGroups(
  current: string,
  device = deviceTimeZone(),
): TimeZoneGroup[] {
  const others = [...new Set([current, ...COMMON_TIME_ZONES])].filter(
    (zone) => zone !== device && isValidTimeZone(zone),
  );
  return [
    { title: "This device", zones: [device] },
    ...(others.length ? [{ title: "Other time zones", zones: others }] : []),
  ];
}
