/**
 * Workspace media cache store — fetches media from the workspace filesystem
 * via the runner's file-read WebSocket channel and caches images/videos for chat rendering.
 */

import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import { sendFilesRead } from '@/services/socket'
import {
  createChunkedTransferStore,
  type ChunkedTransferStore,
} from '@/lib/fileChunks'
import { useNotificationStore } from '@/stores/notifications'

let requestCounter = 0

function nextRequestId(): string {
  return `wsimg-${++requestCounter}-${Date.now()}`
}

type PendingReadRequest = {
  path: string
  kind: 'image' | 'video'
}

/** Maximum simultaneous WebSocket media-fetch requests. */
const MAX_CONCURRENT = 4

const IMAGE_EXTENSIONS = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'tiff', 'tif', 'avif',
])

const VIDEO_EXTENSIONS = new Set([
  'mp4', 'webm', 'ogg', 'ogv', 'mov', 'm4v', 'avi', 'mkv', 'wmv', 'flv', 'mpeg',
  'mpg', '3gp', '3g2', 'ts', 'm2ts',
])

const IMAGE_MIME_MAP: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  svg: 'image/svg+xml',
  bmp: 'image/bmp',
  ico: 'image/x-icon',
  tiff: 'image/tiff',
  tif: 'image/tiff',
  avif: 'image/avif',
}

const VIDEO_MIME_MAP: Record<string, string> = {
  mp4: 'video/mp4',
  webm: 'video/webm',
  ogg: 'video/ogg',
  ogv: 'video/ogg',
  mov: 'video/quicktime',
  m4v: 'video/x-m4v',
  avi: 'video/x-msvideo',
  mkv: 'video/x-matroska',
  wmv: 'video/x-ms-wmv',
  flv: 'video/x-flv',
  mpeg: 'video/mpeg',
  mpg: 'video/mpeg',
  '3gp': 'video/3gpp',
  '3g2': 'video/3gpp2',
  ts: 'video/mp2t',
  m2ts: 'video/mp2t',
}

const VIDEO_READ_MAX_SIZE = 100 * 1024 * 1024 // 100 MB

/** Max buffered base64 chars for one chunked media read (~140 MiB chars). */
const MEDIA_CHUNK_MAX_CHARS = 140 * 1024 * 1024

export type UploadStatus = 'uploading' | 'done' | 'error'

export const useWorkspaceImageStore = defineStore('workspaceImages', () => {
  // path → image data URL
  const imageCache = reactive<Record<string, string>>({})
  // path → object URL for video blobs
  const videoCache = reactive<Record<string, string>>({})
  // path → resolved video MIME type
  const videoMimeTypes = reactive<Record<string, string>>({})

  // requestId → pending read metadata
  const pendingReadRequests = ref<Map<string, PendingReadRequest>>(new Map())

  // Bounded reassembly for chunked media reads (request_id → slices).
  // The chunk store timeout fires the single cleanup callback so stale
  // fetching flags and the concurrency slot never hang.
  let mediaChunks: ChunkedTransferStore | null = null

  function getMediaChunks(): ChunkedTransferStore {
    if (!mediaChunks) {
      mediaChunks = createChunkedTransferStore({
        maxChars: MEDIA_CHUNK_MAX_CHARS,
        onTimeout: (requestId) => {
          const pending = pendingReadRequests.value.get(requestId)
          const path = pending?.path ?? ''
          pendingReadRequests.value.delete(requestId)
          if (path) failPendingMedia(path, pending?.kind)
        },
      })
    }
    return mediaChunks
  }

  // paths currently being fetched
  const fetchingPaths = reactive<Record<string, boolean>>({})
  const fetchingVideos = reactive<Record<string, boolean>>({})

  // request-id-keyed safety timeouts: every media read (image or video)
  // gets a 30 s timer at request time so a silent backend never leaks the
  // fetching flag or the concurrency slot. Timer handles live here (not in
  // the chunk store) and completion always clears them exactly once via
  // clearFetchTimeout(); the chunk-store onTimeout only covers transfers
  // whose timer already fired.
  const MEDIA_FETCH_TIMEOUT_MS = 30_000
  const fetchTimeouts = new Map<string, ReturnType<typeof setTimeout>>()

  // requestId → whether its timeout already fired (prevents double slot
  // release when both the timeout callback and a late result run).
  const timedOutRequests = new Set<string>()

  function armFetchTimeout(requestId: string): void {
    clearFetchTimeout(requestId)
    const timer = setTimeout(() => {
      fetchTimeouts.delete(requestId)
      timedOutRequests.add(requestId)
      const pending = pendingReadRequests.value.get(requestId)
      pendingReadRequests.value.delete(requestId)
      getMediaChunks().cancel(requestId)
      if (pending) failPendingMedia(pending.path, pending.kind)
    }, MEDIA_FETCH_TIMEOUT_MS)
    fetchTimeouts.set(requestId, timer)
  }

  function clearFetchTimeout(requestId: string): void {
    const timer = fetchTimeouts.get(requestId)
    if (timer !== undefined) {
      clearTimeout(timer)
      fetchTimeouts.delete(requestId)
    }
  }

  // Upload state tracking: path → status
  const uploadStatuses = reactive<Record<string, UploadStatus>>({})
  // requestId → path for in-flight uploads
  const pendingUploadIds = ref<Map<string, string>>(new Map())

  // Concurrency control for media reads.
  const fetchQueue: Array<{
    workspaceId: string
    path: string
    kind: 'image' | 'video'
    maxSize?: number
  }> = []
  let activeCount = 0

  function processQueue(): void {
    while (activeCount < MAX_CONCURRENT && fetchQueue.length > 0) {
      const item = fetchQueue.shift()!
      const alreadyLoaded = item.kind === 'image' ? imageCache[item.path] : videoCache[item.path]
      if (alreadyLoaded) {
        if (item.kind === 'image') delete fetchingPaths[item.path]
        else delete fetchingVideos[item.path]
        continue
      }

      activeCount++
      const requestId = nextRequestId()
      pendingReadRequests.value.set(requestId, { path: item.path, kind: item.kind })
      armFetchTimeout(requestId)
      sendFilesRead(item.workspaceId, requestId, item.path, item.maxSize)
    }
  }

  function getExt(path: string): string {
    const name = path.split('/').pop() ?? ''
    const dot = name.lastIndexOf('.')
    return dot >= 0 ? name.slice(dot + 1).toLowerCase() : ''
  }

  function isImagePath(path: string): boolean {
    return IMAGE_EXTENSIONS.has(getExt(path))
  }

  function isVideoPath(path: string): boolean {
    return VIDEO_EXTENSIONS.has(getExt(path))
  }

  function getImageUrl(path: string): string | null {
    return imageCache[path] ?? null
  }

  function getVideoUrl(path: string): string | null {
    return videoCache[path] ?? null
  }

  function getVideoMimeType(path: string): string | null {
    return videoMimeTypes[path] ?? null
  }

  function isFetchingImage(path: string): boolean {
    return fetchingPaths[path] === true
  }

  function isFetchingVideo(path: string): boolean {
    return fetchingVideos[path] === true
  }

  function fetchImage(workspaceId: string, path: string): void {
    if (imageCache[path] || fetchingPaths[path]) return
    fetchingPaths[path] = true
    if (activeCount < MAX_CONCURRENT) {
      activeCount++
      const requestId = nextRequestId()
      pendingReadRequests.value.set(requestId, { path, kind: 'image' })
      armFetchTimeout(requestId)
      sendFilesRead(workspaceId, requestId, path)
    } else {
      fetchQueue.push({ workspaceId, path, kind: 'image' })
    }
  }

  function fetchVideo(workspaceId: string, path: string): void {
    if (videoCache[path] || fetchingVideos[path]) return
    fetchingVideos[path] = true

    if (activeCount < MAX_CONCURRENT) {
      activeCount++
      const requestId = nextRequestId()
      pendingReadRequests.value.set(requestId, { path, kind: 'video' })
      armFetchTimeout(requestId)
      sendFilesRead(workspaceId, requestId, path, VIDEO_READ_MAX_SIZE)
    } else {
      fetchQueue.push({
        workspaceId,
        path,
        kind: 'video',
        maxSize: VIDEO_READ_MAX_SIZE,
      })
    }
  }

  /**
   * Called from useWorkspaceFileEvents when a files:content_result event arrives.
   * Only processes requests initiated by this store. Supports both inline
   * (backward compatible) and chunked (`chunked: true` + prior
   * files:content_chunk slices) payloads.
   */
  function handleContentResult(
    requestId: string,
    path: string,
    content: string,
    error?: string,
    mimeType?: string,
    options: { chunked?: boolean; totalChunks?: number } = {},
  ): void {
    // A late result for an already-timed-out request must never repopulate
    // the cache or release the slot twice: drop it (the timeout already
    // ran failPendingMedia exactly once).
    if (timedOutRequests.has(requestId)) {
      timedOutRequests.delete(requestId)
      pendingReadRequests.value.delete(requestId)
      getMediaChunks().cancel(requestId)
      return
    }
    // Error first: a failed final must clear fetching flags and the
    // concurrency slot (including the request safety timer) before any
    // other handling.
    if (error) {
      const pending = pendingReadRequests.value.get(requestId)
      pendingReadRequests.value.delete(requestId)
      clearFetchTimeout(requestId)
      getMediaChunks().cancel(requestId)
      failPendingMedia(pending?.path ?? path, pending?.kind)
      const notify = useNotificationStore()
      notify.error('Media load failed', error)
      return
    }
    const pending = pendingReadRequests.value.get(requestId)
    const hasChunks = getMediaChunks().has(requestId)
    if (!pending && !hasChunks) return

    if (options.chunked) {
      try {
        const assembled = getMediaChunks().finish(requestId, options.totalChunks)
        const expectedPath = pending?.path ?? assembled.path ?? path
        if (path !== expectedPath || (assembled.path && assembled.path !== expectedPath)) {
          throw new Error(`path mismatch for ${requestId}`)
        }
        pendingReadRequests.value.delete(requestId)
        clearFetchTimeout(requestId)
        storeAssembledMedia(
          expectedPath,
          pending?.kind ?? 'image',
          assembled.content,
          mimeType,
        )
      } catch {
        pendingReadRequests.value.delete(requestId)
        clearFetchTimeout(requestId)
        getMediaChunks().cancel(requestId)
        failPendingMedia(pending?.path ?? path, pending?.kind)
      }
      return
    }

    if (!pending) {
      // Final result for an unknown transfer (e.g. timed-out video fetch):
      // drop it so stale payloads never populate the cache.
      return
    }
    pendingReadRequests.value.delete(requestId)
    clearFetchTimeout(requestId)

    if (error || !content) {
      failPendingMedia(pending.path, pending.kind)
      return
    }

    if (path !== pending.path) {
      failPendingMedia(pending.path, pending.kind)
      return
    }
    storeAssembledMedia(pending.path, pending.kind, content, mimeType)
  }

  /**
   * Called from useWorkspaceFileEvents when a files:content_chunk slice arrives.
   * Chunks carry payload only; authoritative mime comes from the final result.
   * A chunk for the wrong path fails the transfer (with cleanup) instead of
   * being silently dropped, so a stuck spinner can never linger.
   */
  function handleContentChunk(
    requestId: string,
    path: string,
    index: number,
    totalChunks: number,
    content: string,
  ): void {
    if (timedOutRequests.has(requestId)) return
    const pending = pendingReadRequests.value.get(requestId)
    if (!pending && !getMediaChunks().has(requestId)) return
    if (pending && pending.path !== path) {
      pendingReadRequests.value.delete(requestId)
      clearFetchTimeout(requestId)
      getMediaChunks().cancel(requestId)
      failPendingMedia(pending.path, pending.kind)
      return
    }
    try {
      const store = getMediaChunks()
      if (!store.has(requestId)) {
        store.start(requestId, totalChunks, { totalChunks }, pending?.path ?? path)
      }
      store.addChunk(requestId, {
        workspace_id: '',
        request_id: requestId,
        path,
        index,
        total_chunks: totalChunks,
        content,
      })
    } catch {
      pendingReadRequests.value.delete(requestId)
      clearFetchTimeout(requestId)
      getMediaChunks().cancel(requestId)
      failPendingMedia(pending?.path ?? path, pending?.kind)
    }
  }

  function failPendingMedia(path: string, kind?: 'image' | 'video'): void {
    if (kind === 'video' || (!kind && fetchingVideos[path])) {
      delete fetchingVideos[path]
    } else {
      delete fetchingPaths[path]
    }
    activeCount = Math.max(0, activeCount - 1)
    processQueue()
  }

  function storeAssembledMedia(
    path: string,
    kind: 'image' | 'video',
    content: string,
    mimeType?: string,
  ): void {
    if (!content) {
      failPendingMedia(path, kind)
      return
    }

    const cleanBase64 = content.replace(/\s+/g, '')
    if (!cleanBase64) {
      failPendingMedia(path, kind)
      return
    }

    try {
      if (kind === 'image') {
        const ext = getExt(path)
        const mime = mimeType || IMAGE_MIME_MAP[ext] || 'image/png'
        imageCache[path] = `data:${mime};base64,${cleanBase64}`
        delete fetchingPaths[path]
      } else {
        const ext = getExt(path)
        const resolvedMime = mimeType || VIDEO_MIME_MAP[ext] || 'video/mp4'
        let binary: string
        try {
          binary = atob(cleanBase64)
        } catch {
          throw new Error('invalid base64 media payload')
        }
        const bytes = new Uint8Array(binary.length)
        for (let i = 0; i < binary.length; i++) {
          bytes[i] = binary.charCodeAt(i)
        }

        if (videoCache[path]) {
          URL.revokeObjectURL(videoCache[path]!)
        }
        videoCache[path] = URL.createObjectURL(new Blob([bytes], { type: resolvedMime }))
        videoMimeTypes[path] = resolvedMime
        delete fetchingVideos[path]
      }
    } catch {
      failPendingMedia(path, kind)
      const notify = useNotificationStore()
      notify.error('Media load failed', 'The media payload could not be decoded.')
      return
    }

    activeCount = Math.max(0, activeCount - 1)
    processQueue()
  }

  /**
   * Directly store a local data URL for an image path (used for optimistic preview
   * after a local file is dropped/uploaded before the runner confirms).
   */
  function storeLocalImage(path: string, dataUrl: string): void {
    imageCache[path] = dataUrl
    delete fetchingPaths[path]
  }

  /** Register an in-flight upload so the UI can show upload feedback. */
  function trackUpload(requestId: string, path: string): void {
    pendingUploadIds.value.set(requestId, path)
    uploadStatuses[path] = 'uploading'
  }

  /**
   * Called from useWorkspaceFileEvents when a files:upload_result event arrives.
   */
  function handleUploadResult(requestId: string, status: string, error?: string): void {
    const path = pendingUploadIds.value.get(requestId)
    if (!path) return
    pendingUploadIds.value.delete(requestId)

    if (status === 'success' && !error) {
      uploadStatuses[path] = 'done'
      setTimeout(() => {
        if (uploadStatuses[path] === 'done') delete uploadStatuses[path]
      }, 3000)
    } else {
      uploadStatuses[path] = 'error'
      delete imageCache[path]
      if (videoCache[path]) {
        URL.revokeObjectURL(videoCache[path]!)
        delete videoCache[path]
      }
      delete videoMimeTypes[path]
    }
  }

  function getUploadStatus(path: string): UploadStatus | null {
    return uploadStatuses[path] ?? null
  }

  function reset(): void {
    Object.keys(imageCache).forEach((k) => { delete imageCache[k] })
    Object.keys(videoCache).forEach((k) => {
      URL.revokeObjectURL(videoCache[k]!)
      delete videoCache[k]
    })
    Object.keys(videoMimeTypes).forEach((k) => { delete videoMimeTypes[k] })
    Object.keys(fetchingPaths).forEach((k) => { delete fetchingPaths[k] })
    Object.keys(fetchingVideos).forEach((k) => { delete fetchingVideos[k] })
    Object.keys(uploadStatuses).forEach((k) => { delete uploadStatuses[k] })
    fetchTimeouts.forEach((t) => clearTimeout(t))
    fetchTimeouts.clear()
    timedOutRequests.clear()
    pendingReadRequests.value.clear()
    pendingUploadIds.value.clear()
    mediaChunks?.clear()
    fetchQueue.length = 0
    activeCount = 0
  }

  return {
    imageCache,
    videoCache,
    videoMimeTypes,
    fetchingPaths,
    fetchingVideos,
    uploadStatuses,
    isImagePath,
    isVideoPath,
    getImageUrl,
    getVideoUrl,
    getVideoMimeType,
    isFetchingImage,
    isFetchingVideo,
    fetchImage,
    fetchVideo,
    storeLocalImage,
    trackUpload,
    handleUploadResult,
    getUploadStatus,
    handleContentResult,
    handleContentChunk,
    reset,
  }
})
