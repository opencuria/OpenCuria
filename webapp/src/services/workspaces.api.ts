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
  Session,
  PromptIn,
  PromptOut,
  Task,
  Chat,
  ChatCreateIn,
  ChatRenameIn,
  ImageArtifact,
  ImageArtifactCreateIn,
  ImageArtifactCreateOut,
  ImageArtifactCloneIn,
  ImageArtifactCloneOut,
  ImageDefinition,
  RunnerImageBuild,
} from '@/types'
import { get, post, del, patch } from './api'

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

export function promptWorkspace(id: string, data: PromptIn): Promise<PromptOut> {
  return post<PromptOut>(`/workspaces/${id}/prompt/`, data)
}

export function cancelSessionPrompt(workspaceId: string, sessionId: string): Promise<Task> {
  return post<Task>(`/workspaces/${workspaceId}/sessions/${sessionId}/cancel/`)
}

export function stopWorkspace(id: string): Promise<Task> {
  return post<Task>(`/workspaces/${id}/stop/`)
}

export function resumeWorkspace(id: string): Promise<Task> {
  return post<Task>(`/workspaces/${id}/resume/`)
}

export function getWorkspaceSessions(id: string): Promise<Session[]> {
  return get<Session[]>(`/workspaces/${id}/sessions/`)
}

// --- Terminal API ---

export function startTerminal(
  id: string,
  cols: number = 80,
  rows: number = 24,
): Promise<{ task_id: string }> {
  return post<{ task_id: string }>(`/workspaces/${id}/terminal/`, { cols, rows })
}

// --- Desktop API ---

export function startDesktop(id: string): Promise<{ task_id: string }> {
  return post<{ task_id: string }>(`/workspaces/${id}/desktop/`)
}

export function stopDesktop(id: string): Promise<{ task_id: string }> {
  return post<{ task_id: string }>(`/workspaces/${id}/desktop/stop/`)
}

export function getDesktopStatus(
  id: string,
): Promise<{ active: boolean; proxy_url: string | null }> {
  return get<{ active: boolean; proxy_url: string | null }>(
    `/workspaces/${id}/desktop/status/`,
  )
}

export function writeDesktopClipboard(
  id: string,
  text: string,
): Promise<{ text: string }> {
  return post<{ text: string }>(
    `/workspaces/${id}/desktop/clipboard/write/`,
    { text },
  )
}

export function readDesktopClipboard(id: string): Promise<{ text: string }> {
  return post<{ text: string }>(
    `/workspaces/${id}/desktop/clipboard/read/`,
  )
}

// --- Chat API ---

export function listChats(workspaceId: string): Promise<Chat[]> {
  return get<Chat[]>(`/workspaces/${workspaceId}/chats/`)
}

export function createChat(workspaceId: string, data?: ChatCreateIn): Promise<Chat> {
  return post<Chat>(`/workspaces/${workspaceId}/chats/`, data || {})
}

export function renameChat(workspaceId: string, chatId: string, data: ChatRenameIn): Promise<Chat> {
  return patch<Chat>(`/workspaces/${workspaceId}/chats/${chatId}/`, data)
}

export function deleteChat(workspaceId: string, chatId: string): Promise<void> {
  return del<void>(`/workspaces/${workspaceId}/chats/${chatId}/`)
}

export function getChatSessions(workspaceId: string, chatId: string): Promise<Session[]> {
  return get<Session[]>(`/workspaces/${workspaceId}/chats/${chatId}/sessions/`)
}

// --- Workspace image artifact API ---

export function listWorkspaceImageArtifacts(workspaceId: string): Promise<ImageArtifact[]> {
  return get<ImageArtifact[]>(`/workspaces/${workspaceId}/image-artifacts/`)
}

export function createWorkspaceImageArtifact(
  workspaceId: string,
  data: ImageArtifactCreateIn,
): Promise<ImageArtifactCreateOut> {
  return post<ImageArtifactCreateOut>(`/workspaces/${workspaceId}/image-artifacts/`, data)
}

export function deleteWorkspaceImageArtifact(
  workspaceId: string,
  imageArtifactId: string,
): Promise<void> {
  return del<void>(`/workspaces/${workspaceId}/image-artifacts/${imageArtifactId}/`)
}

export function createWorkspaceFromImageArtifact(
  workspaceId: string,
  imageArtifactId: string,
  data: ImageArtifactCloneIn,
): Promise<ImageArtifactCloneOut> {
  return post<ImageArtifactCloneOut>(
    `/workspaces/${workspaceId}/image-artifacts/${imageArtifactId}/workspaces/`,
    data,
  )
}

// --- Global image artifact API ---

export function listUserImageArtifacts(): Promise<ImageArtifact[]> {
  return get<ImageArtifact[]>('/image-artifacts/')
}

export function createImageArtifact(data: ImageArtifactCreateIn): Promise<ImageArtifactCreateOut> {
  return post<ImageArtifactCreateOut>('/image-artifacts/', data)
}

export function deleteImageArtifact(imageArtifactId: string): Promise<void> {
  return del<void>(`/image-artifacts/${imageArtifactId}/`)
}

export function renameImageArtifact(imageArtifactId: string, name: string): Promise<ImageArtifact> {
  return patch<ImageArtifact>(`/image-artifacts/${imageArtifactId}/`, { name })
}

export function createWorkspaceFromUserImageArtifact(
  imageArtifactId: string,
  data: ImageArtifactCloneIn,
): Promise<ImageArtifactCloneOut> {
  return post<ImageArtifactCloneOut>(`/image-artifacts/${imageArtifactId}/workspaces/`, data)
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

export function deleteImageDefinition(id: string): Promise<void> {
  return del<void>(`/image-definitions/${id}/`)
}

export function deactivateImageDefinition(id: string): Promise<ImageDefinition> {
  return post<ImageDefinition>(`/image-definitions/${id}/deactivate/`)
}

export function activateImageDefinition(id: string): Promise<ImageDefinition> {
  return post<ImageDefinition>(`/image-definitions/${id}/activate/`)
}

export function listRunnerImageBuilds(definitionId: string): Promise<RunnerImageBuild[]> {
  return get<RunnerImageBuild[]>(`/image-definitions/${definitionId}/runner-builds/`)
}

export function createRunnerImageBuild(
  definitionId: string,
  data: { runner_id: string; activate?: boolean },
): Promise<RunnerImageBuild> {
  return post<RunnerImageBuild>(`/image-definitions/${definitionId}/runner-builds/`, data)
}

export function updateRunnerImageBuild(
  definitionId: string,
  runnerId: string,
  data: { action: 'activate' | 'deactivate' | 'rebuild' },
): Promise<RunnerImageBuild> {
  return patch<RunnerImageBuild>(
    `/image-definitions/${definitionId}/runner-builds/${runnerId}/`,
    data,
  )
}

export function deleteRunnerImageBuild(definitionId: string, runnerId: string): Promise<void> {
  return del<void>(`/image-definitions/${definitionId}/runner-builds/${runnerId}/`)
}

export function getRunnerImageBuildLog(
  definitionId: string,
  runnerId: string,
): Promise<{ build_log: string }> {
  return get<{ build_log: string }>(
    `/image-definitions/${definitionId}/runner-builds/${runnerId}/log/`,
  )
}
