export interface StorageWorkspace {
  id: string
  name: string
  owner_id: string | null
  owner_label: string
  status: string
  observed_state?: string
  last_activity_at: string | null
}
export interface StorageResource {
  physical_id: string
  kind: string
  managed: boolean
  state: string
  allocated_bytes: number | null
  logical_bytes: number | null
  virtual_bytes: number | null
  shared_bytes: number | null
  reclaimable_bytes: number | null
  aliases: string[]
  dependencies: string[]
  image_id: string | null
  workspace: StorageWorkspace | null
  provenance: string
}
export interface StorageRuntime {
  runtime_type: string
  fresh: boolean
  snapshot_id: number | null
  collected_at: string | null
  received_at: string | null
  filesystems: {
    path: string
    capacity_bytes: number | null
    used_bytes: number | null
    available_bytes: number | null
  }[]
  resources: StorageResource[]
  diagnostics: {
    complete: boolean
    errors: string[]
    foreign_resource_count: number
    collected_at: string
  } | null
}
export interface StorageGeneration {
  id: string
  name: string
  owner_label: string
  runtime_type: string
  definition_id: string | null
  definition_name: string | null
  build_job_id: string | null
  generation: number | null
  status: string
  assignment_status: string | null
  runner_ref: string
  size_bytes: number | null
  size_source: string
  origin_type: string
  revision_id: string | null
  is_legacy: boolean
  is_current: boolean
  is_pending: boolean
  observed_state: string
  dependencies: StorageWorkspace[]
}
export interface RunnerStorage {
  runner_id: string
  runner_online: boolean
  latest_snapshot_id: number | null
  latest_complete: boolean
  runtimes: StorageRuntime[]
  generations: StorageGeneration[]
  operations: {
    task_id: string
    task__status: string
    task__error: string
    phase: string
    target: string
    deliveries: number
  }[]
  capture_requests: {
    id: string
    workspace_id: string
    image_id: string
    phase: string
    diagnostic: string
    resume_suppressed: boolean
  }[]
}
export type DeletionTarget = {
  target_type: 'image' | 'assignment' | 'definition'
  target_id: string
}
export interface DeletionGraph {
  fingerprint: string
  blockers: string[]
  counts: { images: number; workspaces: number }
  images: {
    id: string
    name: string
    owner_id: string | null
    owner_label?: string
    runner_id: string
  }[]
  workspaces: {
    id: string
    name: string
    owner_id: string | null
    owner_label?: string
    runner_id: string
  }[]
}
export interface DeletionRequest extends DeletionTarget {
  id: string
  mode: 'deferred' | 'force'
  phase: string
  diagnostic: string
  can_cancel: boolean
  approval: DeletionGraph
  fingerprint: string
}
export type DispositionAction = 'reconcile' | 'retry' | 'acknowledge_interrupted'
export interface OperationInspection {
  operation_id: string
  status: string
  diagnostic: string
  phase: string
  permitted_actions: DispositionAction[]
  evidence: Record<string, unknown>
}
