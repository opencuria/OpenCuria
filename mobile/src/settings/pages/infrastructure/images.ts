import { retentionLabel } from "@opencuria/chat-core/imageVersions";
import { formatStorageBytes } from "@opencuria/chat-core/workspaceDisk";
import type { CapturedImage, ImageArtifact } from "@opencuria/client";
import { count, formatDate } from "../../format";

const BUSY = new Set(["capturing", "building"]);
const UNDELETABLE = new Set([
  "capturing",
  "building",
  "pending_deletion",
  "deleting",
  "deleted",
]);

/** Whether a version is being captured or built. */
export function isVersionBusy(version: Pick<ImageArtifact, "status">): boolean {
  return BUSY.has(version.status);
}

/** "v3 · Latest", "v2 · Removed when unused" or "v4 · Capturing…". */
export function versionTitle(version: ImageArtifact): string {
  const label = isVersionBusy(version)
    ? version.status === "building"
      ? "Building…"
      : "Capturing…"
    : retentionLabel(version);
  return [`v${version.version ?? "?"}`, label].filter(Boolean).join(" · ");
}

/** "Installed Playwright · Nov 4, 2026 · 2.1 GiB · Used by api". */
export function versionSubtitle(version: ImageArtifact): string {
  const used = version.workspace_count
    ? `Used by ${version.workspaces?.map((workspace) => workspace.name).join(", ") || version.workspace_count}`
    : "";
  return [
    version.message || "No description",
    formatDate(version.created_at),
    formatStorageBytes(version.size_bytes ?? null),
    used,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Older versions of an active image can be deleted; the latest goes with the image. */
export function canDeleteVersion(
  version: ImageArtifact,
  imageActive: boolean,
): boolean {
  return imageActive && !version.is_latest && !UNDELETABLE.has(version.status);
}

/** "v3 · 2 versions · 4.2 GiB · used by 1 workspace · Runner offline". */
export function capturedImageSubtitle(image: CapturedImage): string {
  const capturing = image.versions.some(
    (version) => version.status === "capturing",
  );
  return [
    image.latest_version ? `v${image.latest_version}` : "No ready version",
    count(image.versions.length, "version"),
    formatStorageBytes(image.total_size_bytes ?? null),
    image.workspace_count
      ? `used by ${count(image.workspace_count, "workspace")}`
      : "",
    capturing ? "Capturing…" : "",
    image.status === "pending_deletion" ? "Deleting when unused" : "",
    image.runner_online ? "" : "Runner offline",
  ]
    .filter(Boolean)
    .join(" · ");
}
