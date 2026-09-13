/**
 * useWorkspaceFileEvents — routes workspace file Socket.IO results into
 * the file-explorer and workspace-image stores. Callers own workspace
 * subscribe/unsubscribe; this only registers typed listeners.
 *
 * Chunk events carry payload only; the final result carries authoritative
 * metadata (size/truncated/mime/filename). Both stores validate
 * request/path/total and fail closed on mismatch.
 */
import { onMounted, onUnmounted, toValue, type MaybeRefOrGetter } from 'vue'
import { onEvent } from '@/services/socket'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import type {
  FilesContentChunkEvent,
  FilesContentResultEvent,
  FilesDownloadChunkEvent,
  FilesDownloadResultEvent,
} from '@/types'

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
      onEvent('files:content_result', (data: FilesContentResultEvent) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleContentResult(
          data.request_id,
          data.path,
          data.content,
          data.size,
          data.truncated,
          data.error,
          {
            chunked: data.chunked,
            totalChunks: data.total_chunks,
            mimeType: data.mime_type,
          },
        )
        workspaceImageStore.handleContentResult(
          data.request_id,
          data.path,
          data.content,
          data.error,
          data.mime_type,
          {
            chunked: data.chunked,
            totalChunks: data.total_chunks,
          },
        )
      }),
    )

    cleanupFns.push(
      onEvent('files:content_chunk', (data: FilesContentChunkEvent) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleContentChunk(
          data.request_id,
          data.path,
          data.index,
          data.total_chunks,
          data.content,
        )
        workspaceImageStore.handleContentChunk(
          data.request_id,
          data.path,
          data.index,
          data.total_chunks,
          data.content,
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
      onEvent('files:download_result', (data: FilesDownloadResultEvent) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleDownloadResult(
          data.request_id,
          data.content,
          data.filename,
          data.is_archive,
          data.error,
          {
            chunked: data.chunked,
            totalChunks: data.total_chunks,
            size: data.size,
          },
        )
      }),
    )

    cleanupFns.push(
      onEvent('files:download_chunk', (data: FilesDownloadChunkEvent) => {
        if (data.workspace_id !== currentId()) return
        fileExplorerStore.handleDownloadChunk(
          data.request_id,
          data.path,
          data.index,
          data.total_chunks,
          data.content,
        )
      }),
    )
  })

  onUnmounted(() => {
    cleanupFns.forEach((fn) => fn())
    cleanupFns.length = 0
  })
}
