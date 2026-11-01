import { get, put } from './api'

/** Complete, untruncated UTF-8 content and a compare-and-swap revision token. */
export interface EditorFileContent {
  path: string
  content: string
  revision: string
  exists?: boolean
}

export function readEditorFile(
  workspaceId: string,
  path: string,
  signal?: AbortSignal,
): Promise<EditorFileContent> {
  const params = new URLSearchParams({ path })
  return get<EditorFileContent>(
    `/workspaces/${encodeURIComponent(workspaceId)}/files/content/?${params}`,
    signal,
  )
}

export function saveEditorFile(
  workspaceId: string,
  path: string,
  content: string,
  expectedRevision: string,
): Promise<EditorFileContent> {
  return put<EditorFileContent>(`/workspaces/${encodeURIComponent(workspaceId)}/files/content/`, {
    path,
    content,
    expected_revision: expectedRevision,
  })
}
