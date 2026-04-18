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
  REMOVED = 'removed',
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
}

export enum SessionStatus {
  PENDING = 'pending',
  RUNNING = 'running',
  COMPLETED = 'completed',
  FAILED = 'failed',
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
  RUN_PROMPT = 'run_prompt',
  CANCEL_SESSION = 'cancel_session',
  STOP_WORKSPACE = 'stop_workspace',
  RESUME_WORKSPACE = 'resume_workspace',
  REMOVE_WORKSPACE = 'remove_workspace',
  START_TERMINAL = 'start_terminal',
  CREATE_IMAGE_ARTIFACT = 'create_image_artifact',
  CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT = 'create_workspace_from_image_artifact',
  BUILD_IMAGE = 'build_image',
}

export enum RuntimeType {
  DOCKER = 'docker',
  QEMU = 'qemu',
}

// --- Auth ---

export interface LoginRequest {
  email: string
  password: string
}

export interface RegisterRequest {
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
  created_at: string
}

export interface OrganizationCreateIn {
  name: string
}

// --- Runner ---

export interface Runner {
  id: string
  name: string
  status: RunnerStatus
  available_runtimes: string[]
  qemu_min_vcpus: number
  qemu_max_vcpus: number
  qemu_default_vcpus: number
  qemu_min_memory_mb: number
  qemu_max_memory_mb: number
  qemu_default_memory_mb: number
  qemu_min_disk_size_gb: number
  qemu_max_disk_size_gb: number
  qemu_default_disk_size_gb: number
  qemu_max_active_vcpus: number | null
  qemu_max_active_memory_mb: number | null
  qemu_max_active_disk_size_gb: number | null
  organization_id: string
  connected_at: string | null
  disconnected_at: string | null
  created_at: string
  updated_at: string
}

export interface RunnerCreateIn {
  name: string
}

export interface RunnerCreateOut {
  id: string
  name: string
  api_token: string
}

export interface RunnerUpdateIn {
  qemu_min_vcpus?: number
  qemu_max_vcpus?: number
  qemu_default_vcpus?: number
  qemu_min_memory_mb?: number
  qemu_max_memory_mb?: number
  qemu_default_memory_mb?: number
  qemu_min_disk_size_gb?: number
  qemu_max_disk_size_gb?: number
  qemu_default_disk_size_gb?: number
  qemu_max_active_vcpus?: number | null
  qemu_max_active_memory_mb?: number | null
  qemu_max_active_disk_size_gb?: number | null
}

export interface RunnerSystemMetrics {
  runner_id: string
  timestamp: string
  cpu_usage_percent: number
  ram_used_bytes: number
  ram_total_bytes: number
  disk_used_bytes: number
  disk_total_bytes: number
  vm_metrics?: Record<string, VmSystemMetrics>
}

export interface VmSystemMetrics {
  cpu_usage_percent: number
  ram_used_bytes: number
  ram_total_bytes: number
  disk_used_bytes: number
  disk_total_bytes: number
}

// --- Workspace ---

export interface Workspace {
  id: string
  runner_id: string
  status: WorkspaceStatus
  active_operation: WorkspaceOperation | null
  name: string
  runtime_type: string
  qemu_vcpus: number | null
  qemu_memory_mb: number | null
  qemu_disk_size_gb: number | null
  created_by_id: number
  last_activity_at: string
  auto_stop_timeout_minutes: number | null
  auto_stop_at: string | null
  created_at: string
  updated_at: string
  has_active_session: boolean
  runner_online: boolean
  credential_ids: string[]
}

export type WorkspaceDetail = Workspace

export interface WorkspaceCreateIn {
  name: string
  repos: string[]
  runtime_type?: string
  credential_ids: string[]
  runner_id?: string | null
  qemu_vcpus?: number
  qemu_memory_mb?: number
  qemu_disk_size_gb?: number
  image_id: string
}

export interface WorkspaceUpdateIn {
  name?: string
  credential_ids?: string[]
  qemu_vcpus?: number
  qemu_memory_mb?: number
  qemu_disk_size_gb?: number
}

export interface WorkspaceUpdateOut {
  id: string
  name: string
  updated_at: string
  active_operation: WorkspaceOperation | null
  credential_ids: string[]
  qemu_vcpus: number | null
  qemu_memory_mb: number | null
  qemu_disk_size_gb: number | null
}

export interface WorkspaceCreateOut {
  workspace_id: string
  task_id: string
  status: string
}

// --- Session ---

export interface SessionSkill {
  id: string
  skill_id: string | null
  name: string
  body: string
  created_at: string
}

export interface Session {
  id: string
  workspace_id: string
  chat_id: string | null
  prompt: string
  agent_model: string
  agent_options: Record<string, string>
  output: string
  error_message: string | null
  status: SessionStatus
  read_at: string | null
  status_detail?: string
  created_at: string
  completed_at: string | null
  skills: SessionSkill[]
}

// --- Prompt ---

export interface PromptIn {
  prompt: string
  agent_model?: string
  agent_options?: Record<string, string>
  chat_id?: string
  skill_ids?: string[]
}

export interface PromptOut {
  session_id: string
  task_id: string
  chat_id: string
  status: string
}

// --- Task ---

export interface Task {
  id: string
  runner_id: string
  workspace_id: string | null
  session_id: string | null
  type: TaskType
  status: TaskStatus
  error: string
  created_at: string
  completed_at: string | null
}

// --- Agent ---

export interface AgentOption {
  key: string
  label: string
  choices: string[]
  default: string
}

export interface Agent {
  id: string
  name: string
  description: string
  available_options: AgentOption[]
  default_env: Record<string, string>
  supports_multi_chat: boolean
  has_online_runner: boolean
  required_credential_service_slugs: string[]
  has_credentials: boolean
}

// --- Error ---

export interface ApiError {
  detail: string
  code: string
}

// --- Credentials ---

export interface CredentialService {
  id: string
  name: string
  slug: string
  description: string
  credential_type: string
  env_var_name: string
  target_path: string
  label: string
}

export interface Credential {
  id: string
  name: string
  scope: 'personal' | 'organization'
  service_id: string
  service_name: string
  service_slug: string
  credential_type: string
  env_var_name: string
  target_path: string
  has_public_key: boolean
  created_by_id: number
  created_at: string
  updated_at: string
}

export interface CredentialCreateIn {
  service_id: string
  name?: string
  value?: string
  organization_credential?: boolean
}

export interface CredentialUpdateIn {
  name?: string
  value?: string
}

export interface PublicKeyOut {
  public_key: string
}

// --- Chat ---

export interface Chat {
  id: string
  workspace_id: string
  name: string
  agent_definition_id: string | null
  agent_type: string
  created_at: string
  updated_at: string
  session_count: number
  /** True if this chat has not been persisted to the backend yet (draft state). */
  is_pending?: boolean
}

export interface ChatCreateIn {
  name?: string
  agent_definition_id?: string
}

export interface ChatRenameIn {
  name: string
}

// --- Conversation ---

export interface LastSession {
  id: string
  prompt: string
  status: SessionStatus
  created_at: string
}

export interface Conversation {
  chat_id: string | null
  workspace_id: string
  workspace_name: string
  workspace_status: WorkspaceStatus
  agent_definition_id: string | null
  agent_type: string
  chat_name: string
  last_session: LastSession | null
  session_count: number
  updated_at: string
  is_read: boolean
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

export interface FileEntryRaw {
  name: string
  path: string
  type: 'file' | 'directory'
  size: number
}

export interface FilesListResultEvent {
  workspace_id: string
  request_id: string
  path: string
  entries: FileEntryRaw[]
  error?: string
}

export interface FilesContentResultEvent {
  workspace_id: string
  request_id: string
  path: string
  content: string
  size: number
  truncated: boolean
  mime_type?: string
  error?: string
}

export interface FilesUploadResultEvent {
  workspace_id: string
  request_id: string
  path: string
  status: 'success' | 'error'
  error?: string
}

export interface FilesDownloadResultEvent {
  workspace_id: string
  request_id: string
  path: string
  content: string
  filename: string
  is_archive: boolean
  error?: string
}

// --- Skills ---

export interface Skill {
  id: string
  name: string
  body: string
  scope: 'personal' | 'organization'
  created_by_email: string | null
  created_at: string
  updated_at: string
}

export interface SkillCreateIn {
  name: string
  body: string
  organization_skill?: boolean
}

export interface SkillUpdateIn {
  name?: string
  body?: string
}

// --- API Keys ---

export interface APIKeyPermissionInfo {
  value: string
  label: string
  description: string
  group: string
}

export interface APIKey {
  id: string
  name: string
  key_prefix: string
  is_active: boolean
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  permissions: string[]
}

export interface APIKeyCreatedOut extends APIKey {
  key: string
}

export interface APIKeyCreateIn {
  name: string
  expires_at: string | null
  permissions: string[]
}

export interface APIKeyUpdateIn {
  permissions: string[]
}

// --- Images ---

export interface ImageArtifact {
  id: string
  source_workspace_id: string | null
  runner_artifact_id: string
  name: string
  size_bytes: number | null
  status: 'creating' | 'ready' | 'failed' | 'retired' | 'pending_deletion' | 'deleting' | 'deleted' | 'delete_failed'
  artifact_kind: 'built' | 'captured'
  build_job_id?: string | null
  source_definition_name?: string | null
  source_runner_id?: string | null
  runtime_type?: RuntimeType | 'docker' | 'qemu' | null
  is_deactivated?: boolean
  source_runner_online?: boolean
  delete_requested_at?: string | null
  delete_confirmed_at?: string | null
  delete_last_error?: string
  created_at: string
  created_by_id: number | null
  credential_ids: string[]
}

export interface ImageArtifactCreateIn {
  name: string
  workspace_id?: string
}

export interface ImageArtifactCreateOut {
  task_id: string
  workspace_id: string
}

export interface ImageArtifactCloneIn {
  name?: string
}

export interface ImageArtifactCloneOut {
  workspace_id: string
  task_id: string
  status: string
}

export interface RunnerImageBuild {
  id: string
  image_definition_id: string
  runner_id: string
  image_artifact_id?: string | null
  status: 'pending' | 'building' | 'active' | 'failed' | 'deactivated' | 'pending_deletion' | 'deleting' | 'deleted' | 'delete_failed'
  build_log: string
  build_task_id?: string | null
  built_at?: string | null
  deactivated_at?: string | null
  delete_requested_at?: string | null
  delete_confirmed_at?: string | null
  delete_last_error?: string
  created_at: string
  updated_at: string
}

export interface ImageDefinition {
  id: string
  organization_id: string | null
  created_by_id?: number | null
  name: string
  description: string
  is_standard: boolean
  runtime_type: RuntimeType | 'docker' | 'qemu'
  base_distro: string
  packages: string[]
  env_vars: Record<string, string>
  custom_dockerfile: string
  custom_init_script: string
  is_active: boolean
  status: 'active' | 'deactivated' | 'pending_deletion' | 'deleting' | 'deleted'
  created_at: string
  updated_at: string
}
