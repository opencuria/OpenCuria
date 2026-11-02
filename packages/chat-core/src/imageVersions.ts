/**
 * Image version rules shared by the web and native clients: version labels,
 * reset/update targets and capture targets of a workspace.
 */

export type ImageVersionRetention = "latest" | "kept" | "expires_when_unused";

/** The image version a workspace is (or is being) based on. */
export interface ImageVersionRefInput {
  id: string;
  line_kind: "captured" | "definition" | null;
  line_id: string | null;
  name: string;
  version: number | null;
  message?: string;
  status: string;
  latest_id: string | null;
  latest_version: number | null;
  update_available: boolean;
}

export interface ImageVersionInput {
  id: string;
  status: string;
  version?: number | null;
  retention?: ImageVersionRetention | null;
}

export interface CapturedImageInput {
  id: string;
  name: string;
  status: string;
  runner_id: string;
  versions: Array<{ status: string; version?: number | null }>;
}

export interface RecreateWorkspaceInput {
  base_image?: ImageVersionRefInput | null;
  pending_base_image?: ImageVersionRefInput | null;
}

export interface WorkspaceRecreateOption {
  /** Image version to recreate the workspace from. */
  id: string;
  kind: "reset" | "update";
  version: number | null;
  label: string;
}

/** Select value for "capture as a new image" (instead of a new version). */
export const NEW_IMAGE = "__new__";

/** "name · v3" for the version a workspace is based on. */
export function versionLabel(
  ref: Pick<ImageVersionRefInput, "name" | "version"> | null | undefined,
): string | null {
  if (!ref) return null;
  return ref.version ? `${ref.name} · v${ref.version}` : ref.name;
}

export function retentionLabel(version: ImageVersionInput): string | null {
  if (version.status === "failed") return "Failed";
  if (
    ["pending_deletion", "deleting", "delete_failed"].includes(version.status)
  ) {
    return "Deleting";
  }
  const labels: Record<ImageVersionRetention, string> = {
    latest: "Latest",
    kept: "Kept",
    expires_when_unused: "Removed when unused",
  };
  return version.retention ? labels[version.retention] : null;
}

/** Why a workspace cannot be reset/updated right now; null when it can. */
export function recreateBlocker(
  base: ImageVersionRefInput | null | undefined,
): string | null {
  if (!base)
    return "This workspace predates image tracking and cannot be reset.";
  if (base.status !== "ready" && !base.update_available) {
    return "Its image version is being deleted. Delete the workspace instead.";
  }
  return null;
}

/** A failed reset keeps its target pending; retrying it is the "current" reset. */
export function recreateBase(
  workspace: RecreateWorkspaceInput,
): ImageVersionRefInput | null {
  return workspace.pending_base_image ?? workspace.base_image ?? null;
}

/** Reset to the workspace's own version and/or update to the latest version. */
export function workspaceRecreateOptions(
  workspace: RecreateWorkspaceInput,
): WorkspaceRecreateOption[] {
  const current = recreateBase(workspace);
  if (!current) return [];
  const result: WorkspaceRecreateOption[] = [];
  if (current.status === "ready") {
    result.push({
      id: current.id,
      kind: "reset",
      version: current.version,
      label: `Reset to v${current.version ?? "?"} (current)`,
    });
  }
  if (current.update_available && current.latest_id) {
    result.push({
      id: current.latest_id,
      kind: "update",
      version: current.latest_version,
      label: `Update to v${current.latest_version ?? "?"} (latest)`,
    });
  }
  return result;
}

/** Captured images a workspace can add its next version to (same runner, idle). */
export function captureTargetCandidates<I extends CapturedImageInput>(
  images: I[],
  workspace: { runner_id: string } | null | undefined,
): I[] {
  return images.filter(
    (image) =>
      image.status === "active" &&
      !!workspace &&
      image.runner_id === workspace.runner_id &&
      !image.versions.some((version) => version.status === "capturing"),
  );
}

/** Version number the next capture into an image receives. */
export function nextImageVersion(
  image: Pick<CapturedImageInput, "versions"> | null | undefined,
): number {
  const highest = Math.max(
    0,
    ...(image?.versions ?? []).map((version) => version.version ?? 0),
  );
  return highest + 1;
}

/** Capture into the workspace's own captured image by default, else a new one. */
export function defaultCaptureTarget(
  candidates: Array<Pick<CapturedImageInput, "id">>,
  workspace: { base_image?: ImageVersionRefInput | null } | null | undefined,
): string {
  const base = workspace?.base_image;
  const own =
    base?.line_kind === "captured"
      ? candidates.find((image) => image.id === base.line_id)
      : undefined;
  return own ? own.id : NEW_IMAGE;
}
