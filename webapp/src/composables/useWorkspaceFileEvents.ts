/**
 * useWorkspaceFileEvents — routes workspace file Socket.IO results into
 * the file-explorer and workspace-image stores. Callers own workspace
 * subscribe/unsubscribe; this only registers typed listeners.
 */
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
        fileExplorerStore.handleListResult(data.request_id, data.path, data.entries, data.error)
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
      onEvent('files:content_result', (data) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleContentResult(
          data.request_id,
          data.path,
          data.content,
          data.size,
          data.truncated,
          data.error,
        )
        workspaceImageStore.handleContentResult(
          data.request_id,
          data.path,
          data.content,
          data.error,
          data.mime_type,
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
        )
        workspaceImageStore.handleUploadResult(data.request_id, data.status, data.error)
      }),
    )

    cleanupFns.push(
      onEvent('files:download_result', (data) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleDownloadResult(
          data.request_id,
          data.content,
          data.filename,
          data.is_archive,
          data.error,
        )
      }),
    )
  })

  onUnmounted(() => {
    cleanupFns.forEach((fn) => fn())
    cleanupFns.length = 0
  })
}
