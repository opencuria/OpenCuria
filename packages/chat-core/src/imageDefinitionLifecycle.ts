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

export type RunnerBuildActionId =
  | "build"
  | "rebuild"
  | "activate"
  | "deactivate"
  | "remove"
  | "retry"
  | "retry_remove"
  | "view_log";

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
}

export function isDefinitionHidden(status: string | undefined): boolean {
  return HIDDEN_DEFINITION_STATUSES.has(status || "active");
}

export function isDefinitionLocked(status: string | undefined): boolean {
  return DEFINITION_LOCKED_STATUSES.has(status || "active");
}

export function definitionNeedsPolling(status: string | undefined): boolean {
  return DEFINITION_POLLING_STATUSES.has(status || "");
}

export function buildNeedsPolling(status: string | undefined): boolean {
  return BUILD_POLLING_STATUSES.has(status || "");
}

export { isDefinitionSelectableForWorkspace } from "./workspaceCreation";

export function formatRunnerBuildSummary(
  summary?: ImageDefinitionBuildSummary | null,
): string {
  const counts = summary ?? EMPTY_BUILD_SUMMARY;
  const total =
    counts.active +
    counts.building +
    counts.failed +
    counts.inactive +
    counts.removing;
  if (total === 0) return "Not built on any runner";

  const parts = [
    `${counts.active} active`,
    `${counts.building} building`,
    `${counts.failed} failed`,
  ];
  if (counts.inactive) parts.push(`${counts.inactive} inactive`);
  if (counts.removing) parts.push(`${counts.removing} removing`);
  return parts.join(", ");
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
    };
  }

  const locked = isDefinitionLocked(definition.status);
  return {
    canDuplicate: !locked,
    canEdit: !definition.is_standard && !locked,
    canDelete: !definition.is_standard && !locked,
    canRetryDelete: false,
    canRestore: false,
  };
}

export function getRunnerBuildStatusLabel(
  status: string | null | undefined,
  runnerOnline: boolean,
): string {
  if (!status || status === "deleted") return "Not built";
  if (status === "pending" && !runnerOnline) return "Waiting for runner";
  if (status === "building" && !runnerOnline) return "Runner offline";
  if (status === "pending") return "Queued";
  if (status === "building") return "Building";
  if (status === "active") return "Active";
  if (status === "deactivated") return "Inactive";
  if (status === "failed") return "Failed";
  if (status === "pending_deletion" || status === "deleting") return "Removing";
  if (status === "delete_failed") return "Remove failed";
  return status;
}

export function getRunnerBuildActions(
  status: string | null | undefined,
  options: { definitionLocked?: boolean } = {},
): RunnerBuildAction[] {
  if (options.definitionLocked) return [];
  if (!status || status === "deleted") {
    return [{ id: "build", label: "Build", kind: "primary" }];
  }

  switch (status) {
    case "pending":
    case "building":
      return [{ id: "view_log", label: "View log", kind: "ghost" }];
    case "active":
      return [
        {
          id: "rebuild",
          label: "Rebuild",
          kind: "primary",
          confirm: "rebuild",
        },
        { id: "deactivate", label: "Deactivate", kind: "secondary" },
        {
          id: "remove",
          label: "Remove",
          kind: "destructive",
          confirm: "remove",
        },
      ];
    case "deactivated":
      return [
        { id: "activate", label: "Activate", kind: "primary" },
        {
          id: "rebuild",
          label: "Rebuild",
          kind: "secondary",
          confirm: "rebuild",
        },
        {
          id: "remove",
          label: "Remove",
          kind: "destructive",
          confirm: "remove",
        },
      ];
    case "failed":
      return [
        { id: "retry", label: "Retry", kind: "primary" },
        { id: "view_log", label: "View log", kind: "ghost" },
        {
          id: "remove",
          label: "Remove",
          kind: "destructive",
          confirm: "remove",
        },
      ];
    case "pending_deletion":
    case "deleting":
      return [];
    case "delete_failed":
      return [{ id: "retry_remove", label: "Retry remove", kind: "primary" }];
    default:
      return [{ id: "build", label: "Build", kind: "primary" }];
  }
}

export function visibleImageDefinitions<T extends { status: string }>(
  definitions: T[],
): T[] {
  return definitions.filter(
    (definition) => !isDefinitionHidden(definition.status),
  );
}

export function isInFlightBuild(
  build: { status?: string } | null | undefined,
): boolean {
  return buildNeedsPolling(build?.status);
}

export const BASE_DISTRO_PRESETS = ["ubuntu:22.04", "ubuntu:24.04"];

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

/** A name, a base distro and valid environment lines. */
export function isImageDefinitionFormValid(form: ImageDefinitionForm): boolean {
  return Boolean(
    form.name.trim() && formBaseDistro(form) && envVarsTextValid(form.envVars),
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
