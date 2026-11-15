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
  /** "admin" or "member" in this organization. */
  role: string;
  created_at: string;
  /** Workspace policy; returned by `GET /organizations/{id}/`. */
  workspace_auto_stop_timeout_minutes?: number | null;
  image_versions_to_keep?: number;
}

export interface WorkspacePolicyInput {
  /** `null` keeps inactive workspaces running. */
  workspace_auto_stop_timeout_minutes: number | null;
  image_versions_to_keep: number;
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
  status: "online" | "offline";
  qemu_min_vcpus: number;
  qemu_max_vcpus: number;
  qemu_default_vcpus: number;
  qemu_min_memory_mb: number;
  qemu_max_memory_mb: number;
  qemu_default_memory_mb: number;
  qemu_min_disk_size_gb: number;
  qemu_max_disk_size_gb: number;
  qemu_default_disk_size_gb: number;
  /** Totals across active workspaces; `null` is unlimited. */
  qemu_max_active_vcpus: number | null;
  qemu_max_active_memory_mb: number | null;
  qemu_max_active_disk_size_gb: number | null;
  organization_id: string;
  connected_at: string | null;
  disconnected_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunnerCreateInput {
  name: string;
}

/** The API token is returned once and cannot be retrieved again. */
export interface RunnerCreateResult {
  id: string;
  name: string;
  api_token: string;
}

export interface RunnerUpdateInput {
  qemu_min_vcpus?: number;
  qemu_max_vcpus?: number;
  qemu_default_vcpus?: number;
  qemu_min_memory_mb?: number;
  qemu_max_memory_mb?: number;
  qemu_default_memory_mb?: number;
  qemu_min_disk_size_gb?: number;
  qemu_max_disk_size_gb?: number;
  qemu_default_disk_size_gb?: number;
  qemu_max_active_vcpus?: number | null;
  qemu_max_active_memory_mb?: number | null;
  qemu_max_active_disk_size_gb?: number | null;
}

export interface RunnerSystemMetrics {
  runner_id: string;
  timestamp: string;
  cpu_usage_percent: number;
  ram_used_bytes: number;
  ram_total_bytes: number;
  disk_used_bytes: number;
  disk_total_bytes: number;
  /** Per-workspace VM usage, keyed by workspace id. */
  vm_metrics?: Record<string, VmSystemMetrics>;
}

export interface VmSystemMetrics {
  cpu_usage_percent: number;
  ram_used_bytes: number;
  ram_total_bytes: number;
  disk_used_bytes: number;
  disk_total_bytes: number;
}

export type {
  CredentialService,
  DesktopActivation,
  Plugin,
  PluginCreateIn,
  PluginCredentialRequirement,
  PluginCredentialRequirementIn,
  PluginCredentialServiceType,
  PluginMcpAuthType,
  PluginMcpServer,
  PluginMcpServerIn,
  PluginMcpServerTransport,
  PluginMcpToolPolicy,
  PluginMcpTransport,
  PluginResources,
  PluginSkill,
  PluginSkillIn,
  PluginUpdateIn,
} from "@opencuria/chat-core/plugins";

/** Credential metadata only; secrets never leave the server. */
export interface Credential {
  id: string;
  name: string;
  scope: "personal" | "organization";
  service_id: string;
  service_name: string;
  service_slug: string;
  credential_type: "env" | "file" | "ssh_key" | "mcp_oauth";
  env_var_name: string;
  target_path: string;
  has_public_key: boolean;
  created_by_id: number | null;
  created_at: string;
  updated_at: string;
  oauth_connected: boolean;
  /** "connected", "reconnect_required", "disconnected" or "" (not OAuth). */
  oauth_status: string;
  oauth_reconnect_required: boolean;
  oauth_expires_at: string | null;
}

export interface CredentialCreateInput {
  service_id: string;
  name?: string;
  /** Omitted for SSH keys, which the server generates. */
  value?: string;
  organization_credential?: boolean;
}

/** Only changed fields; an empty value keeps the stored secret. */
export interface CredentialUpdateInput {
  name?: string;
  value?: string;
}

export interface CredentialPublicKey {
  public_key: string;
}

export interface CredentialServiceCreateInput {
  name: string;
  slug?: string;
  description?: string;
  credential_type: "env" | "file" | "ssh_key" | "mcp_oauth";
  env_var_name?: string;
  target_path?: string;
  label?: string;
  oauth_server_url?: string;
}

export interface ApiKeyPermission {
  value: string;
  label: string;
  description: string;
  group: string;
}

/** An API key's metadata; the secret is only returned once on creation. */
export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  is_active: boolean;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  permissions: string[];
}

export interface ApiKeyCreated extends ApiKey {
  key: string;
}

export interface ApiKeyCreateInput {
  name: string;
  /** ISO 8601 with an offset; `null` never expires. */
  expires_at: string | null;
  /** Empty grants every permission that exists at creation. */
  permissions: string[];
}

export type ImageVersionRetention = "latest" | "kept" | "expires_when_unused";

export interface ImageArtifact {
  id: string;
  source_workspace_id: string | null;
  runner_artifact_id: string;
  name: string;
  size_bytes: number | null;
  status:
    | "creating"
    | "capturing"
    | "building"
    | "ready"
    | "failed"
    | "retired"
    | "pending_deletion"
    | "deleting"
    | "deleted"
    | "delete_failed";
  artifact_kind: "built" | "captured";
  build_job_id?: string | null;
  source_definition_name?: string | null;
  source_runner_id?: string | null;
  is_deactivated?: boolean;
  source_runner_online?: boolean;
  delete_requested_at?: string | null;
  delete_confirmed_at?: string | null;
  delete_last_error?: string;
  created_at: string;
  created_by_id: number | null;
  captured_image_id?: string | null;
  version?: number | null;
  message?: string;
  is_latest?: boolean;
  retention?: ImageVersionRetention | null;
  /** How many workspaces use this version; which ones stays private to their owners. */
  workspace_count?: number;
}

/** A versioned captured image with all of its versions (newest first). */
export interface CapturedImage {
  id: string;
  name: string;
  status: "active" | "pending_deletion" | "deleted";
  runner_id: string;
  runner_online: boolean;
  created_by_id: number | null;
  created_at: string;
  latest_id: string | null;
  latest_version: number | null;
  total_size_bytes: number;
  workspace_count: number;
  versions: ImageArtifact[];
}

export interface ImageDefinitionBuildSummary {
  active: number;
  building: number;
  failed: number;
  inactive: number;
  removing: number;
}

export interface ImageDefinition {
  id: string;
  organization_id: string | null;
  created_by_id?: number | null;
  name: string;
  description: string;
  /** A global recipe; only custom definitions can be edited. */
  is_standard: boolean;
  base_distro: string;
  packages: string[];
  env_vars: Record<string, string>;
  custom_init_script: string;
  is_active: boolean;
  status:
    | "active"
    | "deactivated"
    | "pending_deletion"
    | "deleting"
    | "deleted"
    | "delete_failed";
  runner_build_summary?: ImageDefinitionBuildSummary;
  delete_requested_at?: string | null;
  delete_started_at?: string | null;
  delete_confirmed_at?: string | null;
  delete_last_error?: string;
  delete_attempt_count?: number;
  created_at: string;
  updated_at: string;
}

export interface ImageDefinitionInput {
  name: string;
  description: string;
  base_distro: string;
  packages: string[];
  env_vars: Record<string, string>;
  custom_init_script: string;
}

export interface RunnerImageBuild {
  id: string;
  image_definition_id: string;
  runner_id: string;
  image_artifact_id?: string | null;
  current_generation_id?: string | null;
  pending_generation_id?: string | null;
  status:
    | "pending"
    | "building"
    | "active"
    | "failed"
    | "deactivated"
    | "pending_deletion"
    | "deleting"
    | "deleted"
    | "delete_failed";
  /** Only on create/update responses; list responses carry `build_log_size`. */
  build_log?: string;
  build_log_size?: number;
  build_task_id?: string | null;
  built_at?: string | null;
  deactivated_at?: string | null;
  delete_requested_at?: string | null;
  delete_confirmed_at?: string | null;
  delete_last_error?: string;
  created_at: string;
  updated_at: string;
  versions?: ImageArtifact[];
}

export type DeletionTargetType =
  | "image"
  | "assignment"
  | "definition"
  | "captured_image";

export interface DeletionTarget {
  target_type: DeletionTargetType;
  target_id: string;
}

/** Everything a forced deletion would remove, and what blocks it. */
export interface DeletionGraph {
  fingerprint: string;
  blockers: string[];
  counts: { images: number; workspaces: number };
  images: Array<{
    id: string;
    name: string;
    owner_id: string | null;
    owner_label?: string;
    runner_id: string;
  }>;
  workspaces: Array<{
    id: string;
    /** Only the owner gets the workspace name; it is empty for everyone else. */
    name: string;
    owner_id: string | null;
    owner_label?: string;
    runner_id: string;
  }>;
}

export interface DeletionRequest extends DeletionTarget {
  id: string;
  mode: "deferred" | "force";
  /** e.g. "waiting_dependency", "executing", "reconfirmation_required", "completed". */
  phase: string;
  diagnostic: string;
  can_cancel: boolean;
  approval: DeletionGraph;
  fingerprint: string;
  created_at?: string;
  updated_at?: string;
}

export interface RunnerStorageWorkspace {
  id: string;
  /** Only the owner gets the workspace name; it is empty for everyone else. */
  name: string;
  owner_id: string | null;
  owner_label: string;
  status: string;
  observed_state?: string;
  base_image_instance_id?: string | null;
  pending_base_image_instance_id?: string | null;
  allocated_bytes?: number | null;
  last_activity_at: string | null;
}

export interface RunnerStorageResource {
  filesystem_id?: string | null;
  file_identity?: string | null;
  physical_id: string;
  kind: string;
  managed: boolean;
  state: string;
  allocated_bytes: number | null;
  logical_bytes: number | null;
  virtual_bytes: number | null;
  shared_bytes: number | null;
  reclaimable_bytes: number | null;
  aliases: string[];
  dependencies: string[];
  image_id: string | null;
  workspace: RunnerStorageWorkspace | null;
  provenance: string;
}

export interface RunnerStorageRuntime {
  fresh: boolean;
  snapshot_id: number | null;
  collected_at: string | null;
  received_at: string | null;
  filesystems: Array<{
    filesystem_id?: string | null;
    path: string;
    capacity_bytes: number | null;
    used_bytes: number | null;
    available_bytes: number | null;
  }>;
  resources: RunnerStorageResource[];
  diagnostics: {
    complete: boolean;
    errors: string[];
    foreign_resource_count: number;
    collected_at: string;
  } | null;
}

/** One image generation on a runner and the workspaces that depend on it. */
export interface RunnerStorageGeneration {
  id: string;
  name: string;
  owner_label: string;
  definition_id: string | null;
  definition_name: string | null;
  build_job_id: string | null;
  generation: number | null;
  captured_image_id?: string | null;
  line_name?: string | null;
  message?: string;
  is_latest?: boolean;
  retention?: ImageVersionRetention | null;
  status: string;
  assignment_status: string | null;
  runner_ref: string;
  size_bytes: number | null;
  size_source: string;
  origin_type: string;
  revision_id: string | null;
  is_legacy: boolean;
  is_current: boolean;
  is_pending: boolean;
  observed_state: string;
  dependencies: RunnerStorageWorkspace[];
}

export interface RunnerActivity {
  id: string;
  kind: "operation" | "capture" | "recreate" | "deletion";
  title: string;
  target_name: string;
  state: "attention" | "active" | "history";
  outcome: string;
  summary: string;
  diagnostic: string;
  operation_ids: string[];
  created_at: string;
}

export interface RunnerStorage {
  runner_id: string;
  runner_online: boolean;
  latest_snapshot_id: number | null;
  latest_complete: boolean;
  runtimes: RunnerStorageRuntime[];
  generations: RunnerStorageGeneration[];
  activities?: RunnerActivity[];
  operations: Array<{
    task_id: string;
    task__status: string;
    task__error: string;
    /** Only set for the viewer's own workspaces. */
    task__workspace__name?: string | null;
    phase: string;
    target: string;
    deliveries: number;
  }>;
  capture_requests: Array<{
    id: string;
    workspace_id: string;
    image_id: string;
    phase: string;
    diagnostic: string;
    resume_suppressed: boolean;
  }>;
}

export type OperationDisposition =
  | "reconcile"
  | "retry"
  | "acknowledge_interrupted";

export interface OperationInspection {
  operation_id: string;
  status: string;
  diagnostic: string;
  review_message?: string;
  phase: string;
  permitted_actions: OperationDisposition[];
  evidence: Record<string, unknown>;
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
export type {
  ScheduleRecurrence,
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskPatch,
  ScheduledTaskRun,
} from "@opencuria/chat-core/scheduledTasks";

export type {
  AgentConfig,
  AgentConfigInput,
  AgentMode,
  EffortStrategy,
} from "@opencuria/chat-core/harnessAgents";

export interface ProviderConfig {
  small_model: string;
  small_effort: string;
  /** Organization speech default. Empty uses the connected provider default. */
  speech_model?: string;
  /** Dictation text editor. Empty inherits Small Model, then Build. */
  voice_cleanup_model?: string;
}

export type ProviderConfigInput = Partial<ProviderConfig>;

export type {
  ChatGptOAuthStatus,
  ProviderAccount,
  ProviderAccountInput,
  ProviderAccountProvider,
} from "@opencuria/chat-core/providerAccounts";

export interface ChatGptOAuthStart {
  flow_id: string;
  user_code: string;
  verification_url: string;
  /** Seconds between status polls. */
  interval: number;
  expires_in: number;
}

export interface ChatGptOAuthStartInput {
  label?: string;
  shared?: boolean;
  /** Reconnect this existing account. */
  account_id?: string;
}

/** Org-wide nesting limit for subagents (default 2). */
export interface SubagentConfig {
  max_depth: number;
}

/** Org-wide Computer Use limits and optional desktop recording. */
export interface ComputerUseConfig {
  max_steps: number;
  enable_recording: boolean;
  screenshot_max_dimension: number;
}

/** Organization voice capabilities, without a workspace. */
export interface VoiceSettings {
  available: boolean;
  reason?: string;
  models: Array<{ id: string; name: string }>;
  default_model: string;
  cleanup_model: string;
  max_duration_ms: number;
  max_audio_bytes: number;
}

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

/** One published version of an app an agent sent to the user. */
export interface PublishedAppVersion {
  id: string;
  /** Sequential per app, starting at 1. */
  number: number;
  /** Expo config `version`, e.g. "1.2.0". */
  version_name: string;
  /** Expo SDK the build targets, e.g. "57.0.0". */
  sdk_version: string;
  notes: string;
  created_at: string;
  /** Content hash of the icon PNG, downloadable as a version file. */
  icon_sha256: string | null;
  /** Download size in bytes per supported platform. */
  platforms: Partial<Record<"android" | "ios", number>>;
}

/** An Expo app published to the signed-in user, with its newest version. */
export interface PublishedApp {
  id: string;
  name: string;
  slug: string;
  workspace_id: string | null;
  workspace_name: string;
  created_at: string;
  updated_at: string;
  latest_version: PublishedAppVersion | null;
}

export interface PublishedAppDetail extends PublishedApp {
  versions: PublishedAppVersion[];
}

/** A content-addressed file of an app version. */
export interface AppManifestFile {
  sha256: string;
  md5: string;
  size: number;
}

/** An asset the bundle references by its Metro hash `key`. */
export interface AppManifestAsset extends AppManifestFile {
  key: string;
  extension: string;
}

/** Everything a device downloads to install one version on one platform. */
export interface AppManifest {
  app_id: string;
  version_id: string;
  number: number;
  name: string;
  slug: string;
  version_name: string;
  sdk_version: string;
  platform: "android" | "ios";
  /** Public Expo config, exposed to the app as `Constants.expoConfig`. */
  expo_config: Record<string, unknown>;
  bundle: AppManifestFile;
  assets: AppManifestAsset[];
  icon: AppManifestFile | null;
  created_at: string;
}

/** URL and headers for downloading an app file with a native downloader. */
export interface AppFileRequest {
  url: string;
  headers: Record<string, string>;
}

/**
 * Lets a running app reach processes of its workspace. Requests it sends to
 * `http://<process>.<domain>[:port]/<path>` go to
 * `<url>/<process>/<port or 80>/<path>` with `token` in the
 * `X-OpenCuria-App-Session` header.
 */
export interface AppGatewaySession {
  /** Absolute gateway URL of the app on the server. */
  url: string;
  token: string;
  expires_at: string;
  /** Domain whose subdomains name processes: `opencuria.local`. */
  domain: string;
}
