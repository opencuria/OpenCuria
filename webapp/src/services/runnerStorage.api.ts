import { get, post } from './api'
import type {
  RunnerStorage,
  DeletionGraph,
  DeletionRequest,
  DeletionTarget,
  OperationInspection,
  DispositionAction,
} from '@/types/runnerStorage'
export const getRunnerStorage = (id: string) => get<RunnerStorage>(`/runners/${id}/storage/`)
export const refreshRunnerStorage = (id: string) =>
  post<{ requested_at: string }>(`/runners/${id}/storage/refresh/`)
const deletions = '/image-artifacts/deletions/'
export const previewDeletion = (target: DeletionTarget) =>
  post<DeletionGraph>(`${deletions}preview/`, target)
export const requestDeletion = (
  target: DeletionTarget,
  mode: 'deferred' | 'force' = 'deferred',
  fingerprint = '',
) => post<DeletionRequest>(deletions, { ...target, mode, fingerprint })
export const listDeletions = () => get<DeletionRequest[]>(deletions)
export const cancelDeletion = (id: string) => post<DeletionRequest>(`${deletions}${id}/cancel/`)
export const inspectOperation = (id: string) =>
  get<OperationInspection>(`/runners/operations/${id}/`)
export const disposeOperation = (id: string, action: DispositionAction) =>
  post(`/runners/operations/${id}/${action}/`)
