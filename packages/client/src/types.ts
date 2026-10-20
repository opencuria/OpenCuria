import type {
  HarnessConversation,
  HarnessMessage,
  HarnessPartsOut,
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessSession,
  HarnessSteer,
  HarnessTodo,
} from "@opencuria/chat-core/harness";
import type { ProviderModel } from "@opencuria/chat-core/harnessModels";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
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

/** Stable subset of a workspace useful to mobile clients. */
export interface Workspace {
  id: string;
  name: string;
  status: string;
  runner_id: string;
  runtime_type: string;
  active_operation: string | null;
  runner_online: boolean;
  has_active_session: boolean;
  created_at: string;
  updated_at: string;
  [key: string]: unknown;
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

export interface AgentConfig {
  agent: string;
  mode?: string;
  description?: string;
  model: string;
  effort: string;
  inherit_model: boolean;
  effort_strategy: string;
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
  default_model: string;
  small_model: string;
  computer_use_model: string;
  default_effort: string;
  small_effort: string;
  computer_use_effort: string;
}

export type ProviderConfigInput = Partial<ProviderConfig>;

export interface RecentModelUsage {
  model: string;
  effort: string;
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
