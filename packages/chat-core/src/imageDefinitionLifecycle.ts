/**
 * Lifecycle rules of image definitions and their runner builds, shared by
 * the web and native settings, plus the definition form.
 */

export interface ImageDefinitionBuildSummary {
  active: number;
  building: number;
  failed: number;
  inactive: number;
  removing: number;
}

export const EMPTY_BUILD_SUMMARY: ImageDefinitionBuildSummary = {
  active: 0,
  building: 0,
  failed: 0,
  inactive: 0,
  removing: 0,
};

/** How a status reads at a glance. */
export type ImageStatusTone = "success" | "progress" | "danger" | "muted";

/** A lifecycle state as people see it: one short label and its tone. */
export interface ImageStatus {
  label: string;
  tone: ImageStatusTone;
}

/** The fields of a runner build that decide its status and actions. */
export interface RunnerBuildState {
  status?: string | null;
  /** Set while a new version builds; the current version stays in use. */
  pending_generation_id?: string | null;
}

/** The fields of a definition that decide its overall status. */
export interface DefinitionState {
  status: string;
  runner_build_summary?: ImageDefinitionBuildSummary | null;
}

const DEFINITION_LOCKED_STATUSES = new Set([
  "pending_deletion",
  "deleting",
  "deleted",
  "delete_failed",
]);

const DEFINITION_POLLING_STATUSES = new Set(["pending_deletion", "deleting"]);

const BUILD_POLLING_STATUSES = new Set([
  "pending",
  "building",
  "pending_deletion",
  "deleting",
]);

const HIDDEN_DEFINITION_STATUSES = new Set(["deleted"]);

const NOT_BUILT: ImageStatus = { label: "Not built", tone: "muted" };
const BUILDING: ImageStatus = { label: "Building", tone: "progress" };
const READY: ImageStatus = { label: "Ready", tone: "success" };
const DISABLED: ImageStatus = { label: "Disabled", tone: "muted" };
const FAILED: ImageStatus = { label: "Failed", tone: "danger" };
const REMOVING: ImageStatus = { label: "Removing", tone: "progress" };

const RUNNER_BUILD_STATUSES: Record<string, ImageStatus> = {
  // An online runner starts a requested build right away.
  pending: BUILDING,
  building: BUILDING,
  active: READY,
  deactivated: DISABLED,
  failed: FAILED,
  pending_deletion: REMOVING,
  deleting: REMOVING,
  delete_failed: { label: "Removal failed", tone: "danger" },
};

export type RunnerBuildActionId =
  | "build"
  | "rebuild"
  | "activate"
  | "deactivate"
  | "remove"
  | "retry"
  | "retry_remove"
  | "view_log";

/** `primary` is the one next step; the rest are secondary choices. */
export type RunnerBuildActionKind =
  | "primary"
  | "secondary"
  | "destructive"
  | "ghost";

export type RunnerBuildConfirmKind = "rebuild" | "remove";

export interface RunnerBuildAction {
  id: RunnerBuildActionId;
  label: string;
  kind: RunnerBuildActionKind;
  disabled?: boolean;
  confirm?: RunnerBuildConfirmKind;
}

export interface DefinitionActions {
  canDuplicate: boolean;
  canEdit: boolean;
  canDelete: boolean;
  canRetryDelete: boolean;
  canRestore: boolean;
  /** Make a disabled definition available for new workspaces again. */
  canEnable: boolean;
}

export function isDefinitionHidden(status: string | undefined): boolean {
  return HIDDEN_DEFINITION_STATUSES.has(status || "active");
}

export function isDefinitionLocked(status: string | undefined): boolean {
  return DEFINITION_LOCKED_STATUSES.has(status || "active");
}

/** Whether a definition changes on its own: it is deleted, built or removed. */
export function definitionNeedsPolling(
  definition: DefinitionState | null | undefined,
): boolean {
  if (!definition) return false;
  if (DEFINITION_POLLING_STATUSES.has(definition.status)) return true;
  const counts = definition.runner_build_summary;
  return Boolean(counts && (counts.building || counts.removing));
}

/** Whether a runner build changes on its own, including a new version. */
export function buildNeedsPolling(
  build: RunnerBuildState | null | undefined,
): boolean {
  return (
    BUILD_POLLING_STATUSES.has(build?.status || "") ||
    Boolean(build?.pending_generation_id)
  );
}

/** A ready build whose next version is being built. */
export function isBuildUpdating(
  build: RunnerBuildState | null | undefined,
): boolean {
  return build?.status === "active" && Boolean(build.pending_generation_id);
}

export { isDefinitionSelectableForWorkspace } from "./workspaceCreation";

/**
 * The state of one runner build: "Ready" can start new workspaces,
 * "Disabled" is built but hidden from new workspaces.
 */
export function getRunnerBuildStatus(
  build: RunnerBuildState | null | undefined,
  runnerOnline: boolean,
): ImageStatus {
  const status = build?.status;
  if (!status || status === "deleted") return NOT_BUILT;
  if (status === "pending" && !runnerOnline) {
    return { label: "Waiting for runner", tone: "muted" };
  }
  if (status === "building" && !runnerOnline) {
    return { label: "Runner offline", tone: "muted" };
  }
  if (isBuildUpdating(build)) return { label: "Updating", tone: "progress" };
  return RUNNER_BUILD_STATUSES[status] ?? { label: status, tone: "muted" };
}

/**
 * One status for a definition across all runners. The definition's own
 * deletion wins, then work in progress, then failures that need attention.
 */
export function getDefinitionStatus(definition: DefinitionState): ImageStatus {
  switch (definition.status) {
    case "pending_deletion":
    case "deleting":
      return { label: "Deleting", tone: "progress" };
    case "delete_failed":
      return { label: "Delete failed", tone: "danger" };
    case "deactivated":
      return DISABLED;
  }
  const counts = definition.runner_build_summary ?? EMPTY_BUILD_SUMMARY;
  if (counts.building) return BUILDING;
  if (counts.failed) return FAILED;
  if (counts.active) return READY;
  if (counts.removing) return REMOVING;
  if (counts.inactive) return DISABLED;
  return NOT_BUILT;
}

export function getDefinitionActions(definition: {
  is_standard: boolean;
  status: string;
}): DefinitionActions {
  if (definition.status === "delete_failed") {
    return {
      canDuplicate: false,
      canEdit: false,
      canDelete: false,
      canRetryDelete: true,
      canRestore: true,
      canEnable: false,
    };
  }

  const locked = isDefinitionLocked(definition.status);
  const owned = !definition.is_standard && !locked;
  return {
    canDuplicate: !locked,
    canEdit: owned,
    canDelete: owned,
    canRetryDelete: false,
    canRestore: false,
    canEnable: owned && definition.status === "deactivated",
  };
}

const BUILD: RunnerBuildAction = {
  id: "build",
  label: "Build",
  kind: "primary",
};
const BUILD_LOG: RunnerBuildAction = {
  id: "view_log",
  label: "Build log",
  kind: "ghost",
};
const REMOVE: RunnerBuildAction = {
  id: "remove",
  label: "Remove",
  kind: "destructive",
  confirm: "remove",
};

/**
 * Actions on one runner build. The first `primary` action is the next step;
 * while a new version builds, its log replaces Rebuild.
 */
export function getRunnerBuildActions(
  build: RunnerBuildState | null | undefined,
  options: { definitionLocked?: boolean } = {},
): RunnerBuildAction[] {
  if (options.definitionLocked) return [];
  const status = build?.status;
  if (!status || status === "deleted") return [BUILD];

  const rebuild = (kind: RunnerBuildActionKind): RunnerBuildAction =>
    build?.pending_generation_id
      ? BUILD_LOG
      : { id: "rebuild", label: "Rebuild", kind, confirm: "rebuild" };

  switch (status) {
    case "pending":
    case "building":
      return [BUILD_LOG];
    case "active":
      return [
        rebuild("primary"),
        { id: "deactivate", label: "Disable", kind: "secondary" },
        REMOVE,
      ];
    case "deactivated":
      return [
        { id: "activate", label: "Enable", kind: "primary" },
        rebuild("secondary"),
        REMOVE,
      ];
    case "failed":
      return [
        { id: "retry", label: "Retry", kind: "primary" },
        BUILD_LOG,
        REMOVE,
      ];
    case "pending_deletion":
    case "deleting":
      return [];
    case "delete_failed":
      return [{ id: "retry_remove", label: "Retry removal", kind: "primary" }];
    default:
      return [BUILD];
  }
}

export function visibleImageDefinitions<T extends { status: string }>(
  definitions: T[],
): T[] {
  return definitions.filter(
    (definition) => !isDefinitionHidden(definition.status),
  );
}

export const BASE_DISTRO_PRESETS = ["ubuntu:22.04", "ubuntu:24.04"];

/** QEMU builds start from Ubuntu cloud images (`ubuntu:<version>`) only. */
export function isBaseDistroSupported(value: string): boolean {
  return /^ubuntu:\S+$/i.test(value.trim());
}

/** The editable fields of an image definition. */
export interface ImageDefinitionForm {
  name: string;
  description: string;
  /** A preset, or `custom` with `customBaseDistro`. */
  baseDistroPreset: string;
  customBaseDistro: string;
  /** Comma-separated. */
  packages: string;
  /** One `KEY=VALUE` per line. */
  envVars: string;
  customInitScript: string;
}

/** A blank form, or the fields of an existing definition. */
export function imageDefinitionForm(
  definition?: {
    name?: string;
    description?: string;
    base_distro?: string;
    packages?: string[];
    env_vars?: Record<string, string>;
    custom_init_script?: string;
  } | null,
): ImageDefinitionForm {
  const base = definition?.base_distro || "ubuntu:22.04";
  const known = BASE_DISTRO_PRESETS.includes(base);
  return {
    name: definition?.name || "",
    description: definition?.description || "",
    baseDistroPreset: known ? base : "custom",
    customBaseDistro: known ? "" : base,
    packages: (definition?.packages || []).join(", "),
    envVars: Object.entries(definition?.env_vars || {})
      .map(([key, value]) => `${key}=${value}`)
      .join("\n"),
    customInitScript: definition?.custom_init_script || "",
  };
}

/** The base distro the form selects. */
export function formBaseDistro(form: ImageDefinitionForm): string {
  return form.baseDistroPreset === "custom"
    ? form.customBaseDistro.trim()
    : form.baseDistroPreset;
}

/** Every non-blank environment line needs a key before its `=`. */
export function envVarsTextValid(text: string): boolean {
  return text
    .split("\n")
    .every((line) => !line.trim() || line.indexOf("=") > 0);
}

/** A name, a supported base distro and valid environment lines. */
export function isImageDefinitionFormValid(form: ImageDefinitionForm): boolean {
  return Boolean(
    form.name.trim() &&
    isBaseDistroSupported(formBaseDistro(form)) &&
    envVarsTextValid(form.envVars),
  );
}

/** `KEY=VALUE` lines as an object, keys and values trimmed. */
export function parseEnvVarsText(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const index = trimmed.indexOf("=");
    if (index <= 0) continue;
    out[trimmed.slice(0, index).trim()] = trimmed.slice(index + 1).trim();
  }
  return out;
}

/** The create/update body; all six fields are always sent. */
export function imageDefinitionPayload(form: ImageDefinitionForm): {
  name: string;
  description: string;
  base_distro: string;
  packages: string[];
  env_vars: Record<string, string>;
  custom_init_script: string;
} {
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    base_distro: formBaseDistro(form),
    packages: form.packages
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
    env_vars: parseEnvVarsText(form.envVars),
    custom_init_script: form.customInitScript,
  };
}
