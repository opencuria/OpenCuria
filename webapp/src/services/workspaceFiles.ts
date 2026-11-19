/** Workspace file links, uploads and partial reads over signed HTTP URLs. */
import { createWorkspaceFilesApi, type WorkspaceFilesApi } from '@opencuria/client/files'
import { getApiClient } from './api'

let api: WorkspaceFilesApi | null = null

/** The shared file API; links are batched and cached across views. */
export function workspaceFiles(): WorkspaceFilesApi {
  api ??= createWorkspaceFilesApi(getApiClient)
  return api
}

/** Save a signed download URL (files, and folders as `.tar.gz`) without buffering it. */
export function startDownload(url: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  // An empty name keeps the server's `Content-Disposition` filename.
  anchor.download = ''
  anchor.rel = 'noopener'
  document.body.appendChild(anchor)
  try {
    anchor.click()
  } finally {
    anchor.remove()
  }
}
