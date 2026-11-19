/**
 * Chat media the user chose to load. Bytes stream from signed links and the
 * browser HTTP cache keeps them; only the tap decision lives here, per
 * workspace for the session, so re-renders and scrolling never ask again.
 */
import { defineStore } from 'pinia'
import { reactive } from 'vue'

export const useWorkspaceMediaStore = defineStore('workspaceMedia', () => {
  const requested = reactive(new Set<string>())
  const key = (workspaceId: string, path: string) => `${workspaceId}\0${path}`

  function isRequested(workspaceId: string, path: string): boolean {
    return requested.has(key(workspaceId, path))
  }

  function request(workspaceId: string, path: string): void {
    requested.add(key(workspaceId, path))
  }

  return { isRequested, request }
})
