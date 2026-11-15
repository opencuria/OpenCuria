/**
 * Workspace creation and resource rules shared by the web and native clients:
 * which images can start a workspace, on which runners, with which resources.
 */

export interface RunnerResourceInput {
  id: string;
  name: string;
  status: string;
  qemu_min_vcpus: number;
  qemu_max_vcpus: number;
  qemu_default_vcpus: number;
  qemu_min_memory_mb: number;
  qemu_max_memory_mb: number;
  qemu_default_memory_mb: number;
  qemu_min_disk_size_gb: number;
  qemu_max_disk_size_gb: number;
  qemu_default_disk_size_gb: number;
}

export interface ImageDefinitionOptionInput {
  id: string;
  name: string;
  is_active: boolean;
  status: string;
}

export interface RunnerImageBuildOptionInput {
  runner_id: string;
  status: string;
  image_artifact_id?: string | null;
}

export interface CapturedImageArtifactInput {
  id: string;
  status: string;
  source_runner_id?: string | null;
}

export interface CapturedImageOptionInput<
  A extends CapturedImageArtifactInput = CapturedImageArtifactInput,
> {
  id: string;
  name: string;
  status: string;
  runner_online: boolean;
  latest_id: string | null;
  latest_version: number | null;
  versions: A[];
}

/** An image a new workspace can start from. */
export type WorkspaceImageOption<
  B extends RunnerImageBuildOptionInput = RunnerImageBuildOptionInput,
  A extends CapturedImageArtifactInput = CapturedImageArtifactInput,
> =
  | {
      kind: "definition";
      value: string;
      label: string;
      definitionId: string;
      /** Active builds on online runners. */
      runnerBuilds: B[];
    }
  | {
      kind: "captured";
      value: string;
      label: string;
      capturedImageId: string;
      /** Latest ready version; captured clones always start from it. */
      imageArtifact: A;
    };

export interface WorkspaceImageOptions<
  B extends RunnerImageBuildOptionInput = RunnerImageBuildOptionInput,
  A extends CapturedImageArtifactInput = CapturedImageArtifactInput,
> {
  /** Organization images built from definitions. */
  definitions: Array<
    Extract<WorkspaceImageOption<B, A>, { kind: "definition" }>
  >;
  /** The user's captured images. */
  captured: Array<Extract<WorkspaceImageOption<B, A>, { kind: "captured" }>>;
}

export interface QemuResourceLimits {
  minVcpus: number;
  maxVcpus: number;
  defaultVcpus: number;
  minMemoryMb: number;
  maxMemoryMb: number;
  defaultMemoryMb: number;
  minDiskSizeGb: number;
  maxDiskSizeGb: number;
  defaultDiskSizeGb: number;
}

export interface QemuResources {
  vcpus: number;
  memoryMb: number;
  diskSizeGb: number;
}

/** Limits assumed for creation before a runner is known. */
export const DEFAULT_QEMU_LIMITS: QemuResourceLimits = {
  minVcpus: 1,
  maxVcpus: 8,
  defaultVcpus: 2,
  minMemoryMb: 1024,
  maxMemoryMb: 16384,
  defaultMemoryMb: 4096,
  minDiskSizeGb: 20,
  maxDiskSizeGb: 200,
  defaultDiskSizeGb: 50,
};

/** Permissive limits when editing a workspace whose runner is not listed; the server validates. */
export const UNLISTED_RUNNER_QEMU_LIMITS: QemuResourceLimits = {
  minVcpus: 1,
  maxVcpus: 64,
  defaultVcpus: 2,
  minMemoryMb: 512,
  maxMemoryMb: 262144,
  defaultMemoryMb: 4096,
  minDiskSizeGb: 10,
  maxDiskSizeGb: 2000,
  defaultDiskSizeGb: 50,
};

export const DEFAULT_DESKTOP_WIDTH = 1920;
export const DEFAULT_DESKTOP_HEIGHT = 1080;
export const DESKTOP_SIZE_LIMITS = {
  minWidth: 800,
  maxWidth: 3840,
  minHeight: 600,
  maxHeight: 2160,
} as const;

const WORKSPACE_HIDDEN_DEFINITION_STATUSES = new Set([
  "deleted",
  "pending_deletion",
  "deleting",
  "delete_failed",
]);

/** Whether new workspaces may start from an image definition. */
export function isDefinitionSelectableForWorkspace(
  definition: Pick<ImageDefinitionOptionInput, "is_active" | "status">,
): boolean {
  return (
    definition.is_active &&
    !WORKSPACE_HIDDEN_DEFINITION_STATUSES.has(definition.status)
  );
}

function isOnline(runner: Pick<RunnerResourceInput, "status">): boolean {
  return runner.status === "online";
}

/** Images a new workspace can start from right now, grouped by origin. */
export function workspaceImageOptions<
  B extends RunnerImageBuildOptionInput,
  A extends CapturedImageArtifactInput,
>(input: {
  definitions: ImageDefinitionOptionInput[];
  buildsByDefinition: Record<string, B[] | undefined>;
  capturedImages: Array<CapturedImageOptionInput<A>>;
  runners: Array<Pick<RunnerResourceInput, "id" | "status">>;
}): WorkspaceImageOptions<B, A> {
  const online = input.runners.filter(isOnline);
  const definitions: WorkspaceImageOptions<B, A>["definitions"] = [];
  for (const definition of input.definitions) {
    if (!isDefinitionSelectableForWorkspace(definition)) continue;
    const runnerBuilds = (input.buildsByDefinition[definition.id] ?? []).filter(
      (build) =>
        build.status === "active" &&
        !!build.image_artifact_id &&
        online.some((runner) => runner.id === build.runner_id),
    );
    if (!runnerBuilds.length) continue;
    definitions.push({
      kind: "definition",
      value: `definition:${definition.id}`,
      label: definition.name,
      definitionId: definition.id,
      runnerBuilds,
    });
  }
  const captured: WorkspaceImageOptions<B, A>["captured"] = [];
  for (const image of input.capturedImages) {
    const artifact = image.versions.find(
      (version) => version.id === image.latest_id,
    );
    if (!artifact || image.status !== "active") continue;
    const sourceRunner = artifact.source_runner_id
      ? input.runners.find((runner) => runner.id === artifact.source_runner_id)
      : null;
    if (artifact.status !== "ready" || !image.runner_online || !sourceRunner)
      continue;
    captured.push({
      kind: "captured",
      value: `captured:${artifact.id}`,
      label: `${image.name} · v${image.latest_version}`,
      capturedImageId: image.id,
      imageArtifact: artifact,
    });
  }
  return { definitions, captured };
}

/** The preselected image: the first organization image, else the first captured one. */
export function defaultWorkspaceImageOption<
  D extends { value: string },
  C extends { value: string },
>(options: { definitions: D[]; captured: C[] }): D | C | null {
  return options.definitions[0] ?? options.captured[0] ?? null;
}

/** Online runners a workspace from this image can be placed on. */
export function compatibleRunners<
  R extends Pick<RunnerResourceInput, "id" | "status">,
>(option: WorkspaceImageOption | null | undefined, runners: R[]): R[] {
  if (!option) return [];
  if (option.kind === "definition") {
    return option.runnerBuilds.flatMap((build) => {
      const runner = runners.find(
        (entry) => entry.id === build.runner_id && isOnline(entry),
      );
      return runner ? [runner] : [];
    });
  }
  const source = option.imageArtifact.source_runner_id;
  const runner = source ? runners.find((entry) => entry.id === source) : null;
  return runner && isOnline(runner) ? [runner] : [];
}

/** Image artifact a definition-based workspace starts from on the chosen runner. */
export function definitionArtifactForRunner(
  option: WorkspaceImageOption | null | undefined,
  runnerId: string | null | undefined,
): string | null {
  if (!option || option.kind !== "definition") return null;
  const selected = runnerId || option.runnerBuilds[0]?.runner_id;
  if (!selected) return null;
  return (
    option.runnerBuilds.find((build) => build.runner_id === selected)
      ?.image_artifact_id ?? null
  );
}

/** QEMU limits and defaults of a runner. */
export function qemuResourceLimits(
  runner: RunnerResourceInput | null | undefined,
  fallback: QemuResourceLimits = DEFAULT_QEMU_LIMITS,
): QemuResourceLimits {
  if (!runner) return fallback;
  return {
    minVcpus: runner.qemu_min_vcpus,
    maxVcpus: runner.qemu_max_vcpus,
    defaultVcpus: runner.qemu_default_vcpus,
    minMemoryMb: runner.qemu_min_memory_mb,
    maxMemoryMb: runner.qemu_max_memory_mb,
    defaultMemoryMb: runner.qemu_default_memory_mb,
    minDiskSizeGb: runner.qemu_min_disk_size_gb,
    maxDiskSizeGb: runner.qemu_max_disk_size_gb,
    defaultDiskSizeGb: runner.qemu_default_disk_size_gb,
  };
}

/**
 * Carry a resource value over to another runner. A value still at the
 * previous default was never customized, so it follows the new default; a
 * value outside the new limits falls back to the new default as well.
 */
export function fitQemuResource(
  value: number,
  min: number,
  max: number,
  previousDefault: number,
  nextDefault: number,
): number {
  if (Number.isNaN(value) || value === previousDefault) return nextDefault;
  if (value < min || value > max) return nextDefault;
  return value;
}

/** Re-fit all resources when the effective runner (and its limits) changes. */
export function fitQemuResources(
  resources: QemuResources,
  previous: QemuResourceLimits,
  next: QemuResourceLimits,
): QemuResources {
  return {
    vcpus: fitQemuResource(
      resources.vcpus,
      next.minVcpus,
      next.maxVcpus,
      previous.defaultVcpus,
      next.defaultVcpus,
    ),
    memoryMb: fitQemuResource(
      resources.memoryMb,
      next.minMemoryMb,
      next.maxMemoryMb,
      previous.defaultMemoryMb,
      next.defaultMemoryMb,
    ),
    diskSizeGb: fitQemuResource(
      resources.diskSizeGb,
      next.minDiskSizeGb,
      next.maxDiskSizeGb,
      previous.defaultDiskSizeGb,
      next.defaultDiskSizeGb,
    ),
  };
}

/** Whether all resources are integers within the runner's limits. */
export function areQemuResourcesValid(
  resources: QemuResources,
  limits: QemuResourceLimits,
): boolean {
  const { vcpus, memoryMb, diskSizeGb } = resources;
  return (
    [vcpus, memoryMb, diskSizeGb].every(Number.isInteger) &&
    vcpus >= limits.minVcpus &&
    vcpus <= limits.maxVcpus &&
    memoryMb >= limits.minMemoryMb &&
    memoryMb <= limits.maxMemoryMb &&
    diskSizeGb >= limits.minDiskSizeGb &&
    diskSizeGb <= limits.maxDiskSizeGb
  );
}

/** Desktop sizes are even integers within the supported range. */
export function isDesktopSizeValid(width: number, height: number): boolean {
  return (
    Number.isInteger(width) &&
    Number.isInteger(height) &&
    width >= DESKTOP_SIZE_LIMITS.minWidth &&
    width <= DESKTOP_SIZE_LIMITS.maxWidth &&
    width % 2 === 0 &&
    height >= DESKTOP_SIZE_LIMITS.minHeight &&
    height <= DESKTOP_SIZE_LIMITS.maxHeight &&
    height % 2 === 0
  );
}

export interface WorkspaceSettingsForm extends QemuResources {
  name: string;
  credentialIds: string[];
  pluginIds: string[];
  desktopWidth: number;
  desktopHeight: number;
}

export interface WorkspaceSettingsInput {
  name: string;
  credential_ids: string[];
  plugin_ids: string[];
  qemu_vcpus: number | null;
  qemu_memory_mb: number | null;
  qemu_disk_size_gb: number | null;
  desktop_width?: number | null;
  desktop_height?: number | null;
}

export interface WorkspaceUpdatePayload {
  name: string;
  credential_ids: string[];
  plugin_ids: string[];
  qemu_vcpus?: number;
  qemu_memory_mb?: number;
  qemu_disk_size_gb?: number;
  desktop_width?: number;
  desktop_height?: number;
}

/** Current settings of a workspace as an editable form. */
export function workspaceSettingsForm(
  workspace: WorkspaceSettingsInput,
  limits: QemuResourceLimits,
): WorkspaceSettingsForm {
  return {
    name: workspace.name,
    credentialIds: [...workspace.credential_ids],
    pluginIds: [...workspace.plugin_ids],
    vcpus: workspace.qemu_vcpus ?? limits.defaultVcpus,
    memoryMb: workspace.qemu_memory_mb ?? limits.defaultMemoryMb,
    diskSizeGb: workspace.qemu_disk_size_gb ?? limits.defaultDiskSizeGb,
    desktopWidth: workspace.desktop_width || DEFAULT_DESKTOP_WIDTH,
    desktopHeight: workspace.desktop_height || DEFAULT_DESKTOP_HEIGHT,
  };
}

/**
 * PATCH body for a settings form: name, credentials and plugins always, and
 * only those resources that differ from the workspace's effective values
 * (resource changes restart a running workspace).
 */
export function workspaceUpdatePayload(
  workspace: WorkspaceSettingsInput,
  form: WorkspaceSettingsForm,
  limits: QemuResourceLimits,
): WorkspaceUpdatePayload {
  const current = workspaceSettingsForm(workspace, limits);
  const payload: WorkspaceUpdatePayload = {
    name: form.name.trim(),
    credential_ids: [...form.credentialIds],
    plugin_ids: [...form.pluginIds],
  };
  if (form.vcpus !== current.vcpus) payload.qemu_vcpus = form.vcpus;
  if (form.memoryMb !== current.memoryMb)
    payload.qemu_memory_mb = form.memoryMb;
  if (form.diskSizeGb !== current.diskSizeGb)
    payload.qemu_disk_size_gb = form.diskSizeGb;
  if (form.desktopWidth !== current.desktopWidth)
    payload.desktop_width = form.desktopWidth;
  if (form.desktopHeight !== current.desktopHeight)
    payload.desktop_height = form.desktopHeight;
  return payload;
}

/** Whether a settings form differs from the workspace's current settings. */
export function isWorkspaceSettingsChanged(
  workspace: WorkspaceSettingsInput,
  form: WorkspaceSettingsForm,
  limits: QemuResourceLimits,
): boolean {
  const current = workspaceSettingsForm(workspace, limits);
  const sameIds = (left: string[], right: string[]) =>
    left.length === right.length && left.every((id) => right.includes(id));
  return (
    form.name.trim() !== current.name ||
    !sameIds(form.credentialIds, current.credentialIds) ||
    !sameIds(form.pluginIds, current.pluginIds) ||
    form.vcpus !== current.vcpus ||
    form.memoryMb !== current.memoryMb ||
    form.diskSizeGb !== current.diskSizeGb ||
    form.desktopWidth !== current.desktopWidth ||
    form.desktopHeight !== current.desktopHeight
  );
}
