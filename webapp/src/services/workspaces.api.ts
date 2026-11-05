/**
 * Workspace REST API service.
 */

import type {
  Workspace,
  WorkspaceDetail,
  WorkspaceCreateIn,
  WorkspaceCreateOut,
  WorkspaceUpdateIn,
  WorkspaceUpdateOut,
  WorkspaceProcess,
  Task,
  ImageArtifactCreateIn,
  ImageArtifactCreateOut,
  ImageArtifactCloneIn,
  ImageArtifactCloneOut,
  ImageDefinition,
  RunnerImageBuild,
  CapturedImage,
  WorkspaceRecreateOut,
} from '@/types'
import { createDesktopApi } from '@opencuria/client/desktop'
import type { DesktopStatus, DesktopViewerIntent } from '@opencuria/client/desktop'
import { get, post, del, patch, getApiClient, postDuringClipboardGesture } from './api'

export function listWorkspaces(runnerId?: string): Promise<Workspace[]> {
  const query = runnerId ? `?runner_id=${runnerId}` : ''
  return get<Workspace[]>(`/workspaces/${query}`)
}

export function getWorkspace(id: string): Promise<WorkspaceDetail> {
  return get<WorkspaceDetail>(`/workspaces/${id}/`)
}

export function createWorkspace(data: WorkspaceCreateIn): Promise<WorkspaceCreateOut> {
  return post<WorkspaceCreateOut>('/workspaces/', {
    ...data,
    image_artifact_id: data.image_id,
  })
}

export function updateWorkspace(id: string, data: WorkspaceUpdateIn): Promise<WorkspaceUpdateOut> {
  return patch<WorkspaceUpdateOut>(`/workspaces/${id}/`, data)
}

export function deleteWorkspace(id: string): Promise<Task> {
  return del<Task>(`/workspaces/${id}/`)
}

export function stopWorkspace(id: string): Promise<Task> {
  return post<Task>(`/workspaces/${id}/stop/`)
}

export function resumeWorkspace(id: string): Promise<Task> {
  return post<Task>(`/workspaces/${id}/resume/`)
}

/** Reset (own version) or update (latest version); workspace data is lost. */
export function recreateWorkspace(id: string, imageId: string): Promise<WorkspaceRecreateOut> {
  return post<WorkspaceRecreateOut>(`/workspaces/${id}/recreate/`, { image_id: imageId })
}

// --- Terminal API ---

/** `clientId` is the realtime connection that owns the shell; it closes with it. */
export function startTerminal(
  id: string,
  cols: number = 80,
  rows: number = 24,
  clientId: string | null = null,
): Promise<{ task_id: string }> {
  return post<{ task_id: string }>(`/workspaces/${id}/terminal/`, {
    cols,
    rows,
    client_id: clientId ?? undefined,
  })
}

// --- Desktop API (shared with mobile: `@opencuria/client/desktop`) ---

export type { DesktopStatus, DesktopViewerIntent }

const desktopApi = createDesktopApi(getApiClient)

export function startDesktop(
  id: string,
  intent: DesktopViewerIntent,
  signal?: AbortSignal,
): Promise<{ task_id: string }> {
  return desktopApi.start(id, intent, signal)
}

export function stopDesktop(id: string, intent: DesktopViewerIntent): Promise<{ task_id: string }> {
  return desktopApi.stop(id, intent)
}

export function renewDesktop(
  id: string,
  intent: DesktopViewerIntent,
  signal?: AbortSignal,
): Promise<DesktopStatus> {
  return desktopApi.renew(id, intent, signal)
}

export function getDesktopStatus(
  id: string,
  viewerClientId?: string,
  signal?: AbortSignal,
): Promise<DesktopStatus> {
  return desktopApi.status(id, viewerClientId, signal)
}

export function takeDesktopControl(id: string): Promise<{ aborted_session_ids: string[] }> {
  return desktopApi.takeControl(id)
}

export function writeDesktopClipboard(id: string, text: string): Promise<{ text: string }> {
  return desktopApi.writeClipboard(id, text)
}

export function readDesktopClipboard(id: string): Promise<{ text: string }> {
  return desktopApi.readClipboard(id)
}

/** Atomically copy the VM selection without leaving the browser's copy gesture. */
export function copyDesktopSelectionDuringGesture(
  id: string,
  context: Window,
  shift = false,
): string {
  const response = postDuringClipboardGesture<{ text: unknown }>(
    `/workspaces/${id}/desktop/clipboard/read/`,
    { copy_selection: true, shift },
    context,
  )
  if (typeof response?.text !== 'string') throw new Error('Invalid clipboard response')
  return response.text
}

// --- Background process API (workspace-bound) ---

export function listProcesses(workspaceId: string): Promise<WorkspaceProcess[]> {
  return get<WorkspaceProcess[]>(`/workspaces/${workspaceId}/processes/`)
}

/** Fetch processes reconciled against the runner's current live state. */
export function listLiveProcesses(workspaceId: string): Promise<WorkspaceProcess[]> {
  return get<WorkspaceProcess[]>(`/workspaces/${workspaceId}/processes/?live=true`)
}

export function stopProcess(
  workspaceId: string,
  processId: string,
): Promise<WorkspaceProcess> {
  return post<WorkspaceProcess>(
    `/workspaces/${workspaceId}/processes/${processId}/stop/`,
  )
}

export function restartProcess(
  workspaceId: string,
  processId: string,
): Promise<WorkspaceProcess> {
  return post<WorkspaceProcess>(
    `/workspaces/${workspaceId}/processes/${processId}/restart/`,
  )
}

export function deleteProcess(
  workspaceId: string,
  processId: string,
): Promise<void> {
  return del<void>(`/workspaces/${workspaceId}/processes/${processId}/`)
}

// --- Workspace image artifact API ---

export function createWorkspaceImageArtifact(
  workspaceId: string,
  data: ImageArtifactCreateIn,
): Promise<ImageArtifactCreateOut> {
  return post<ImageArtifactCreateOut>(`/workspaces/${workspaceId}/image-artifacts/`, data)
}

// --- Captured image (versioned) API ---

export function listCapturedImages(): Promise<CapturedImage[]> {
  return get<CapturedImage[]>('/captured-images/')
}

export function renameCapturedImage(id: string, name: string): Promise<CapturedImage> {
  return patch<CapturedImage>(`/captured-images/${id}/`, { name })
}

/** New workspaces always use the latest version of a captured image. */
export function createWorkspaceFromCapturedImage(
  id: string,
  data: ImageArtifactCloneIn,
): Promise<ImageArtifactCloneOut> {
  return post<ImageArtifactCloneOut>(`/captured-images/${id}/workspaces/`, data)
}

// --- Image definition APIs ---

export function listImageDefinitions(): Promise<ImageDefinition[]> {
  return get<ImageDefinition[]>('/image-definitions/')
}

export function createImageDefinition(data: Partial<ImageDefinition>): Promise<ImageDefinition> {
  return post<ImageDefinition>('/image-definitions/', data)
}

export function duplicateImageDefinition(
  id: string,
  data?: { name?: string },
): Promise<ImageDefinition> {
  return post<ImageDefinition>(`/image-definitions/${id}/duplicate/`, data || {})
}

export function updateImageDefinition(id: string, data: Partial<ImageDefinition>): Promise<ImageDefinition> {
  return patch<ImageDefinition>(`/image-definitions/${id}/`, data)
}

export function activateImageDefinition(id: string): Promise<ImageDefinition> {
  return post<ImageDefinition>(`/image-definitions/${id}/activate/`)
}

export function listRunnerImageBuilds(definitionId: string): Promise<RunnerImageBuild[]> {
  return get<RunnerImageBuild[]>(`/image-definitions/${definitionId}/runner-builds/`)
}

export function createRunnerImageBuild(
  definitionId: string,
  data: { runner_id: string; activate?: boolean; message?: string },
): Promise<RunnerImageBuild> {
  return post<RunnerImageBuild>(`/image-definitions/${definitionId}/runner-builds/`, data)
}

export function updateRunnerImageBuild(
  definitionId: string,
  runnerId: string,
  data: { action: 'activate' | 'deactivate' | 'rebuild'; message?: string },
): Promise<RunnerImageBuild> {
  return patch<RunnerImageBuild>(
    `/image-definitions/${definitionId}/runner-builds/${runnerId}/`,
    data,
  )
}

export function getRunnerImageBuildLog(
  definitionId: string,
  runnerId: string,
): Promise<{ build_log: string }> {
  return get<{ build_log: string }>(
    `/image-definitions/${definitionId}/runner-builds/${runnerId}/log/`,
  )
}
