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
  ImageCaptureInput,
  ImageCaptureResult,
  WorkspaceRecreateOut,
} from '@/types'
import { createDesktopApi } from '@opencuria/client/desktop'
import { createImagesApi } from '@opencuria/client/settings'
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
  data: ImageCaptureInput,
): Promise<ImageCaptureResult> {
  return post<ImageCaptureResult>(`/workspaces/${workspaceId}/image-artifacts/`, data)
}

// --- Captured images, image definitions and runner builds ---

export const {
  listCaptured: listCapturedImages,
  renameCaptured: renameCapturedImage,
  createWorkspace: createWorkspaceFromCapturedImage,
  listDefinitions: listImageDefinitions,
  createDefinition: createImageDefinition,
  duplicateDefinition: duplicateImageDefinition,
  updateDefinition: updateImageDefinition,
  activateDefinition: activateImageDefinition,
  listRunnerBuilds: listRunnerImageBuilds,
  createRunnerBuild: createRunnerImageBuild,
  updateRunnerBuild: updateRunnerImageBuild,
  runnerBuildLog: getRunnerImageBuildLog,
} = createImagesApi(getApiClient)
