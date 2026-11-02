import type {
  HarnessConversation,
  HarnessId,
  HarnessMessage,
  HarnessPartsOut,
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSessionMode,
  HarnessSteer,
  HarnessTodo,
} from "@opencuria/chat-core/harness";
import type { ProviderModel } from "@opencuria/chat-core/harnessModels";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
}

export interface PushCapabilities {
  ios: boolean;
  android: boolean;
  live_activities: boolean;
}

export interface MobileDeviceRegistration {
  platform: "ios" | "android";
  app_id: string;
  apns_environment: "sandbox" | "production";
  push_token: string;
  notifications_enabled: boolean;
  live_activities_enabled: boolean;
}

export interface MobileDevice {
  id: string;
  notifications_enabled: boolean;
  live_activities_enabled: boolean;
}

export interface AgentActivityRow {
  sessionId: string;
  workspaceId: string;
  title: string;
  workspaceName: string;
  phase:
    | "running"
    | "waiting_for_approval"
    | "waiting_for_input"
    | "completed"
    | "error"
    | "interrupted";
}

export interface AgentActivitySnapshot {
  organizationId: string;
  deviceId: string;
  activeCount: number;
  activities: AgentActivityRow[];
  updatedAt: string;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
  role: string;
  created_at: string;
}

export interface AuthUser {
  id: number;
  email: string;
  first_name: string;
  last_name: string;
  organizations: Organization[];
}

/** Last observed filesystem usage inside a workspace (`/workspace`). */
export interface WorkspaceDiskUsage {
  used_bytes: number;
  free_bytes: number;
  total_bytes: number;
  observed_at: string;
}

/** The image version a workspace is (or is being) based on. */
export interface ImageVersionRef {
  id: string;
  line_kind: "captured" | "definition" | null;
  line_id: string | null;
  name: string;
  version: number | null;
  message: string;
  status: string;
  latest_id: string | null;
  latest_version: number | null;
  update_available: boolean;
}

/** Workspace as returned by `GET /workspaces/`; newer fields are optional for older servers. */
export interface Workspace {
  id: string;
  name: string;
  status: string;
  runner_id: string;
  active_operation: string | null;
  runner_online: boolean;
  has_active_session: boolean;
  created_at: string;
  updated_at: string;
  intervention_required?: boolean;
  lifecycle_diagnostic?: string;
  qemu_vcpus?: number | null;
  qemu_memory_mb?: number | null;
  qemu_disk_size_gb?: number | null;
  disk_usage?: WorkspaceDiskUsage | null;
  desktop_width?: number;
  desktop_height?: number;
  last_activity_at?: string;
  auto_stop_at?: string | null;
  delete_last_error?: string;
  credential_ids?: string[];
  plugin_ids?: string[];
  credentials_present?: boolean;
  base_image_name?: string | null;
  base_image?: ImageVersionRef | null;
  pending_base_image?: ImageVersionRef | null;
  repos?: string[];
  [key: string]: unknown;
}

export interface WorkspaceCreateInput {
  name: string;
  /** Image artifact to start from; sent as `image_artifact_id`. */
  image_id: string;
  repos?: string[];
  credential_ids?: string[];
  plugin_ids?: string[];
  runner_id?: string | null;
  qemu_vcpus?: number;
  qemu_memory_mb?: number;
  qemu_disk_size_gb?: number;
  desktop_width?: number;
  desktop_height?: number;
}

export interface WorkspaceCreateResult {
  workspace_id: string;
  task_id: string;
  status: string;
  plugin_ids: string[];
}

export interface WorkspaceUpdateInput {
  name?: string;
  credential_ids?: string[];
  plugin_ids?: string[];
  qemu_vcpus?: number;
  qemu_memory_mb?: number;
  qemu_disk_size_gb?: number;
  desktop_width?: number;
  desktop_height?: number;
}

/** PATCH result; credential sync may still be pending on the runner. */
export interface WorkspaceUpdateResult {
  id: string;
  name: string;
  updated_at: string;
  active_operation: string | null;
  credential_ids: string[];
  plugin_ids: string[];
  credentials_present: boolean;
  credential_sync_status: "synced" | "pending" | "failed" | "not_required";
  credential_sync_detail?: string | null;
  qemu_vcpus: number | null;
  qemu_memory_mb: number | null;
  qemu_disk_size_gb: number | null;
  desktop_width: number;
  desktop_height: number;
}

export interface WorkspaceTask {
  id: string;
  workspace_id: string | null;
  type: string;
  status: string;
  error: string;
}

export interface WorkspaceRecreateResult {
  workspace_id: string;
  task_id: string;
  active_operation: string | null;
}

/** Capture as a new image (`name`) or as the next version of `captured_image_id`. */
export interface ImageCaptureInput {
  name?: string;
  captured_image_id?: string | null;
  message?: string;
}

export interface ImageCaptureResult {
  task_id: string;
  workspace_id: string;
}

export interface Runner {
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
  [key: string]: unknown;
}

export interface PluginCredentialRequirement {
  id: string;
  key: string;
  description: string;
  required: boolean;
  service_id: string;
  service_name: string;
  service_slug: string;
  credential_type: string;
}

export interface Plugin {
  id: string;
  name: string;
  slug: string;
  description: string;
  enabled: boolean;
  published: boolean;
  organization_id: string | null;
  is_global: boolean;
  org_enabled: boolean;
  credential_requirements: PluginCredentialRequirement[];
  [key: string]: unknown;
}

/** Credential metadata only; secrets never leave the server. */
export interface Credential {
  id: string;
  name: string;
  scope: "personal" | "organization";
  service_id: string;
  service_name: string;
  service_slug: string;
  credential_type: string;
  created_by_id: number;
  oauth_connected: boolean;
  [key: string]: unknown;
}

export interface ImageArtifact {
  id: string;
  name: string;
  status: string;
  artifact_kind: "built" | "captured";
  source_runner_id?: string | null;
  captured_image_id?: string | null;
  version?: number | null;
  message?: string;
  created_at: string;
  [key: string]: unknown;
}

/** A versioned captured image with all of its versions (newest first). */
export interface CapturedImage {
  id: string;
  name: string;
  status: string;
  runner_id: string;
  runner_online: boolean;
  latest_id: string | null;
  latest_version: number | null;
  workspace_count: number;
  versions: ImageArtifact[];
  [key: string]: unknown;
}

export interface ImageDefinition {
  id: string;
  name: string;
  description: string;
  is_active: boolean;
  status: string;
  [key: string]: unknown;
}

export interface RunnerImageBuild {
  id: string;
  image_definition_id: string;
  runner_id: string;
  image_artifact_id?: string | null;
  status: string;
  [key: string]: unknown;
}

/** Workspace from the latest version of a captured image; runner and resources come from it. */
export interface CapturedImageWorkspaceInput {
  name?: string;
  credential_ids?: string[];
  plugin_ids?: string[];
}

export interface Skill {
  id: string;
  name: string;
  body: string;
  scope: "personal" | "organization";
  created_by_email: string | null;
  created_at: string;
  updated_at: string;
}

export interface SkillCreateInput {
  name: string;
  body: string;
  organization_skill?: boolean;
}

export interface SkillUpdateInput {
  name?: string;
  body?: string;
}

export type { HarnessEngine } from "@opencuria/chat-core/harnessModels";

export type AgentMode = "primary" | "subagent";
export type EffortStrategy =
  | "fixed"
  | "inherit"
  | "lowest"
  | "medium"
  | "highest";

export interface AgentConfig {
  agent: string;
  mode: AgentMode;
  description: string;
  model: string;
  effort: string;
  inherit_model: boolean;
  effort_strategy: EffortStrategy;
}

export interface AgentConfigInput {
  agent: string;
  description?: string;
  model?: string;
  effort?: string;
  inherit_model?: boolean;
  effort_strategy?: string;
}

export interface ProviderConfig {
  small_model: string;
  small_effort: string;
  /** Organization speech default. Empty uses the connected provider default. */
  speech_model?: string;
  /** Dictation text editor. Empty inherits Small Model, then Build. */
  voice_cleanup_model?: string;
}

export type ProviderConfigInput = Partial<ProviderConfig>;

export interface RecentModelUsage {
  model: string;
  effort: string;
  /** Absent on older backends. */
  harness_id?: HarnessId;
  mode?: HarnessSessionMode;
  last_used_at: string;
}

export interface HarnessTimeline {
  session: HarnessSession;
  messages: HarnessMessage[];
  permissions?: HarnessPermissionRequest[];
  questions?: HarnessQuestionRequest[];
  /** Queued (not yet delivered) steers; absent on older backends. */
  steers?: HarnessSteer[];
}

export interface Todo extends HarnessTodo {}
export type {
  HarnessConversation,
  HarnessMessage,
  HarnessPartsOut,
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSteer,
  HarnessTodo,
  ProviderModel,
};

export interface FileEntry {
  name: string;
  path: string;
  type: "file" | "directory";
  size: number;
  [key: string]: unknown;
}

export interface FileListResult {
  workspace_id: string;
  request_id: string;
  path: string;
  entries: FileEntry[];
  error?: string;
}

export interface FileFindPath {
  /** Full workspace-absolute path returned by the runner (never just a name). */
  path: string;
  name: string;
}

/** Actual runner/backend Socket.IO payload shape. */
export interface FileFindResultEvent {
  workspace_id: string;
  request_id: string;
  query: string;
  paths: Array<FileFindPath | string>;
  truncated: boolean;
  error?: string;
}

/** Public client result; paths are normalized to full workspace-absolute strings. */
export interface FileFindResult extends Omit<FileFindResultEvent, "paths"> {
  paths: string[];
}

export interface FileContentResult {
  workspace_id: string;
  request_id: string;
  path: string;
  content: string;
  size: number;
  truncated: boolean;
  mime_type?: string;
  chunked?: boolean;
  total_chunks?: number;
  error?: string;
}

export interface FileDownloadResult {
  workspace_id: string;
  request_id: string;
  path: string;
  content: string;
  size: number;
  filename: string;
  is_archive: boolean;
  chunked?: boolean;
  total_chunks?: number;
  error?: string;
}

export interface FileUploadResult {
  workspace_id: string;
  request_id: string;
  path: string;
  status: "success" | "error" | string;
  error?: string;
}

export interface FileChunkEvent {
  workspace_id: string;
  request_id: string;
  path: string;
  index: number;
  total_chunks: number;
  content: string;
}
