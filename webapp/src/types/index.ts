// ---------------------------------------------------------------------------
// TypeScript type definitions mirroring backend Pydantic schemas & enums.
// ---------------------------------------------------------------------------

// --- Enums ---

export enum RunnerStatus {
  ONLINE = 'online',
  OFFLINE = 'offline',
}

export enum WorkspaceStatus {
  CREATING = 'creating',
  RUNNING = 'running',
  STOPPED = 'stopped',
  FAILED = 'failed',
  PENDING_DELETION = 'pending_deletion',
  DELETING = 'deleting',
  DELETED = 'deleted',
  DELETE_FAILED = 'delete_failed',
}

export enum WorkspaceOperation {
  CREATING = 'creating',
  STARTING = 'starting',
  STOPPING = 'stopping',
  RESTARTING = 'restarting',
  REMOVING = 'removing',
  CAPTURING_IMAGE = 'capturing_image',
  RESETTING = 'resetting',
  UPDATING = 'updating',
  SYNCHRONIZING_CREDENTIALS = 'synchronizing_credentials',
}

export enum TaskStatus {
  PENDING = 'pending',
  IN_PROGRESS = 'in_progress',
  COMPLETED = 'completed',
  FAILED = 'failed',
}

export enum TaskType {
  CREATE_WORKSPACE = 'create_workspace',
  UPDATE_WORKSPACE = 'update_workspace',
  STOP_WORKSPACE = 'stop_workspace',
  RESUME_WORKSPACE = 'resume_workspace',
  REMOVE_WORKSPACE = 'remove_workspace',
  START_TERMINAL = 'start_terminal',
  CREATE_IMAGE_ARTIFACT = 'create_image_artifact',
  CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT = 'create_workspace_from_image_artifact',
  BUILD_IMAGE = 'build_image',
}

// --- Shared REST DTOs (one definition for web and mobile) ---

export type {
  ApiKey,
  ApiKeyCreated,
  ApiKeyCreateInput,
  ApiKeyPermission,
  CapturedImage,
  CapturedImageWorkspaceInput,
  Credential,
  CredentialCreateInput,
  CredentialServiceCreateInput,
  CredentialUpdateInput,
  ImageArtifact,
  ImageCaptureInput,
  ImageCaptureResult,
  ImageDefinition,
  ImageDefinitionInput,
  Runner,
  RunnerCreateResult,
  RunnerImageBuild,
  RunnerSystemMetrics,
  RunnerUpdateInput,
  Skill,
  SkillCreateInput,
  SkillUpdateInput,
  VmSystemMetrics,
} from '@opencuria/client/types'
import type { ImageArtifact } from '@opencuria/client/types'

// --- Auth ---

export interface LoginRequest {
  email: string
  password: string
}

export interface TokenPair {
  access_token: string
  refresh_token: string
}

export interface RefreshRequest {
  refresh_token: string
}

export interface SsoCallbackRequest {
  code: string
  redirect_uri: string
}

export interface SsoProvider {
  enabled: boolean
  provider: string | null
  authorization_endpoint: string | null
  client_id: string | null
  scope: string | null
  supports_pkce: boolean
}

export interface AuthProviders {
  password_enabled: boolean
  sso: SsoProvider
}

export interface User {
  id: number
  email: string
  first_name: string
  last_name: string
}

export interface UserWithOrgs extends User {
  organizations: UserOrg[]
}

export interface UserOrg {
  id: string
  name: string
  slug: string
  role: string
  created_at: string
}

// --- Organization ---

export interface Organization {
  id: string
  name: string
  slug: string
  role: string
  workspace_auto_stop_timeout_minutes: number | null
  image_versions_to_keep: number
  created_at: string
}

// --- Runner ---

// --- Workspace ---

/** Last observed filesystem usage inside a workspace (`/workspace`). */
export interface WorkspaceDiskUsage {
  used_bytes: number
  free_bytes: number
  total_bytes: number
  observed_at: string
}

export interface Workspace {
  intervention_required?: boolean
  lifecycle_diagnostic?: string
  id: string
  runner_id: string
  status: WorkspaceStatus
  active_operation: WorkspaceOperation | null
  name: string
  qemu_vcpus: number | null
  qemu_memory_mb: number | null
  qemu_disk_size_gb: number | null
  disk_usage?: WorkspaceDiskUsage | null
  desktop_width: number
  desktop_height: number
  created_by_id: number
  last_activity_at: string
  auto_stop_timeout_minutes: number | null
  auto_stop_at: string | null
  delete_requested_at: string | null
  delete_started_at: string | null
  delete_confirmed_at: string | null
  delete_last_error: string
  delete_attempt_count: number
  created_at: string
  updated_at: string
  has_active_session: boolean
  runner_online: boolean
  credential_ids: string[]
  plugin_ids: string[]
  credentials_present: boolean
  base_image_name?: string | null
  base_image?: ImageVersionRef | null
  pending_base_image?: ImageVersionRef | null
  repos?: string[]
}

/** The image version a workspace is (or is being) based on. */
export interface ImageVersionRef {
  id: string
  line_kind: 'captured' | 'definition' | null
  line_id: string | null
  name: string
  version: number | null
  message: string
  status: ImageArtifact['status']
  latest_id: string | null
  latest_version: number | null
  update_available: boolean
}

export interface WorkspaceRecreateOut {
  workspace_id: string
  task_id: string
  active_operation: WorkspaceOperation | null
}

export type WorkspaceDetail = Workspace

export interface WorkspaceCreateIn {
  name: string
  repos: string[]
  credential_ids: string[]
  plugin_ids: string[]
  runner_id?: string | null
  qemu_vcpus?: number
  qemu_memory_mb?: number
  qemu_disk_size_gb?: number
  desktop_width?: number
  desktop_height?: number
  image_id: string
}

export interface WorkspaceUpdateIn {
  name?: string
  credential_ids?: string[]
  plugin_ids?: string[]
  qemu_vcpus?: number
  qemu_memory_mb?: number
  qemu_disk_size_gb?: number
  desktop_width?: number
  desktop_height?: number
}

export interface WorkspaceUpdateOut {
  id: string
  name: string
  updated_at: string
  active_operation: WorkspaceOperation | null
  credential_ids: string[]
  plugin_ids: string[]
  credentials_present: boolean
  credential_sync_status: 'synced' | 'pending' | 'failed' | 'not_required'
  credential_sync_detail?: string | null
  qemu_vcpus: number | null
  qemu_memory_mb: number | null
  qemu_disk_size_gb: number | null
  desktop_width: number
  desktop_height: number
}

export interface WorkspaceCreateOut {
  workspace_id: string
  task_id: string
  status: string
  plugin_ids: string[]
}

// --- Task ---

export interface Task {
  id: string
  runner_id: string
  workspace_id: string | null
  type: TaskType
  status: TaskStatus
  error: string
  created_at: string
  completed_at: string | null
}

// --- Credentials ---

export interface OAuthConnectOut {
  authorization_url: string
  status: 'pending'
}

// --- File Explorer ---

export interface FileNode {
  name: string
  path: string
  type: 'file' | 'directory'
  size: number
  children?: FileNode[]
  isExpanded?: boolean
}

export type { FileEntry as FileEntryRaw } from '@opencuria/client/types'

export interface FilesUploadResultEvent {
  workspace_id: string
  request_id: string
  path: string
  status: 'success' | 'error'
  error?: string
}

// --- Background processes ---

export enum ProcessStatus {
  RUNNING = 'running',
  EXITED = 'exited',
  KILLED = 'killed',
  FAILED = 'failed',
}

export enum ProcessKind {
  PERSISTENT = 'persistent',
  TEMP = 'temp',
}

export interface WorkspaceProcess {
  id: string
  workspace_id: string
  name: string
  command: string
  workdir: string
  pid: number | null
  log_path: string
  status: ProcessStatus | string
  exit_code: number | null
  run_count: number
  kind: ProcessKind | string
  started_at: string
  ended_at: string | null
  updated_at: string
}

export interface ProcessStatusChangedEvent {
  workspace_id: string
  process_id: string
  status: string
  exit_code: number | null
  pid: number | null
  log_path?: string
  run_count?: number
}

export interface ProcessRemovedEvent {
  workspace_id: string
  process_id: string
}
