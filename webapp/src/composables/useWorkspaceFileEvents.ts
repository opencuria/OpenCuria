/** Route directory, search and upload events; the shared client owns reads/downloads. */
import { onMounted, onUnmounted, toValue, type MaybeRefOrGetter } from 'vue'
import { onEvent } from '@/services/socket'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'

export function useWorkspaceFileEvents(workspaceId: MaybeRefOrGetter<string>): void {
  const fileExplorerStore = useFileExplorerStore()
  const workspaceImageStore = useWorkspaceImageStore()
  const cleanupFns: (() => void)[] = []

  function currentId(): string {
    return toValue(workspaceId)
  }

  onMounted(() => {
    cleanupFns.push(
      onEvent('files:list_result', (data) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleListResult(
          data.request_id,
          data.path,
          data.entries,
          data.error,
          data.workspace_id,
          true,
        )
      }),
    )

    cleanupFns.push(
      onEvent('files:find_result', (data) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleFindResult(
          data.request_id,
          (data.paths ?? []).map((entry) => entry.path),
          data.error,
        )
      }),
    )

    cleanupFns.push(
      onEvent('files:upload_result', (data) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleUploadResult(
          data.request_id,
          data.path,
          data.status,
          currentId(),
          data.error,
          fileExplorerStore.shouldRefreshUploadResult(data.request_id),
        )
        workspaceImageStore.handleUploadResult(data.request_id, data.status, data.error)
      }),
    )
  })

  onUnmounted(() => {
    cleanupFns.forEach((fn) => fn())
    cleanupFns.length = 0
  })
}
