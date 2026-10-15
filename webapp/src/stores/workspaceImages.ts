/**
 * Workspace media cache store — fetches media from the workspace filesystem
 * via the runner's file-read WebSocket channel and caches images/videos for chat rendering.
 */

import { defineStore } from 'pinia'
import { nextTick, ref, reactive } from 'vue'
import { sendFilesRead } from '@/services/socket'
import { createChunkedTransferStore, type ChunkedTransferStore } from '@/lib/fileChunks'
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
  'png',
  'jpg',
  'jpeg',
  'gif',
  'webp',
  'svg',
  'bmp',
  'ico',
  'tiff',
  'tif',
  'avif',
])

const VIDEO_EXTENSIONS = new Set([
  'mp4',
  'webm',
  'ogg',
  'ogv',
  'mov',
  'm4v',
  'avi',
  'mkv',
  'wmv',
  'flv',
  'mpeg',
  'mpg',
  '3gp',
  '3g2',
  'ts',
  'm2ts',
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

/** Maximum retained media payload size, excluding protected visible media. */
export const MEDIA_CACHE_BUDGET_BYTES = 32 * 1024 * 1024

/** Base64 strings are retained as UTF-16 JavaScript strings. */
const BASE64_STORAGE_BYTES_PER_CHAR = 2

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

  type MediaEntry = {
    kind: 'image' | 'video'
    url: string
    bytes: number
    lastUsed: number
  }
  const mediaEntries = new Map<string, MediaEntry>()
  const mediaPins = new Map<string, number>()
  const mediaGeneration = ref(0)
  const retiredVideoUrls = new Map<string, Set<string>>()
  const failedUploadPaths = new Set<string>()
  let mediaUseCounter = 0

  function mediaUrl(path: string): string | undefined {
    return imageCache[path] ?? videoCache[path]
  }

  function estimateDataUrlBytes(url: string): number {
    return url.length * BASE64_STORAGE_BYTES_PER_CHAR
  }

  function trackMedia(path: string, kind: 'image' | 'video', url: string, bytes: number): void {
    const previous = mediaEntries.get(path)
    if (previous?.kind === 'video' && previous.url !== url) {
      if ((mediaPins.get(path) ?? 0) > 0) {
        const retired = retiredVideoUrls.get(path) ?? new Set<string>()
        retired.add(previous.url)
        retiredVideoUrls.set(path, retired)
      } else {
        URL.revokeObjectURL(previous.url)
      }
    }
    mediaEntries.set(path, {
      kind,
      url,
      bytes,
      lastUsed: ++mediaUseCounter,
    })
  }

  function syncMediaEntry(path: string): MediaEntry | undefined {
    const image = imageCache[path]
    const video = videoCache[path]
    const url = image ?? video
    const kind = image ? 'image' : video ? 'video' : undefined
    if (!url || !kind) {
      mediaEntries.delete(path)
      return undefined
    }
    const known = mediaEntries.get(path)
    if (known?.url === url && known.kind === kind) return known
    const bytes = kind === 'video' ? (known?.bytes ?? 0) : estimateDataUrlBytes(url)
    trackMedia(path, kind, url, bytes)
    return mediaEntries.get(path)
  }

  function trimMediaCache(): void {
    // Reconcile direct cache assignments used by legacy callers/tests. This
    // sweep runs on mutations/releases, never on render getters.
    const paths = new Set([...Object.keys(imageCache), ...Object.keys(videoCache)])
    for (const path of mediaEntries.keys()) {
      if (!paths.has(path)) mediaEntries.delete(path)
    }
    for (const path of paths) syncMediaEntry(path)

    let totalBytes = [...mediaEntries.values()].reduce((sum, entry) => sum + entry.bytes, 0)
    if (totalBytes <= MEDIA_CACHE_BUDGET_BYTES) return

    const candidates = [...mediaEntries.entries()]
      .filter(([path]) => (mediaPins.get(path) ?? 0) === 0)
      .sort(([, left], [, right]) => left.lastUsed - right.lastUsed)
    for (const [path, entry] of candidates) {
      if (totalBytes <= MEDIA_CACHE_BUDGET_BYTES) break
      if (entry.kind === 'image') delete imageCache[path]
      else {
        delete videoCache[path]
        delete videoMimeTypes[path]
        URL.revokeObjectURL(entry.url)
      }
      mediaEntries.delete(path)
      totalBytes -= entry.bytes
    }
  }

  function releaseMediaPath(path: string): void {
    const count = mediaPins.get(path) ?? 0
    if (count <= 1) {
      mediaPins.delete(path)
      for (const url of retiredVideoUrls.get(path) ?? []) URL.revokeObjectURL(url)
      retiredVideoUrls.delete(path)
      if (failedUploadPaths.delete(path)) removeMedia(path)
    } else {
      mediaPins.set(path, count - 1)
    }
  }

  /** Release multiple visible-media pins and trim the cache once. */
  function releaseMedia(paths: Iterable<string>): void {
    for (const path of paths) releaseMediaPath(path)
    trimMediaCache()
  }

  function pinMedia(path: string, pin: boolean): void {
    if (!pin) {
      releaseMedia([path])
      return
    }
    const count = mediaPins.get(path) ?? 0
    mediaPins.set(path, count + 1)
    syncMediaEntry(path)
  }

  function removeMedia(path: string): void {
    if ((mediaPins.get(path) ?? 0) > 0) return
    delete imageCache[path]
    const video = videoCache[path]
    if (video) {
      delete videoCache[path]
      URL.revokeObjectURL(video)
    }
    delete videoMimeTypes[path]
    mediaEntries.delete(path)
  }

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
          if (!pending) return
          pendingReadRequests.value.delete(requestId)
          clearFetchTimeout(requestId)
          failPendingMedia(pending.path, pending.kind)
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

  function armFetchTimeout(requestId: string): void {
    clearFetchTimeout(requestId)
    const timer = setTimeout(() => {
      fetchTimeouts.delete(requestId)
      const pending = pendingReadRequests.value.get(requestId)
      if (!pending) return
      pendingReadRequests.value.delete(requestId)
      getMediaChunks().cancel(requestId)
      failPendingMedia(pending.path, pending.kind)
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
  const uploadStatusTimeouts = new Map<string, ReturnType<typeof setTimeout>>()
  const uploadPinnedPaths = new Set<string>()
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
    const entry = mediaEntries.get(path)
    if (entry && imageCache[path] === entry.url) entry.lastUsed = ++mediaUseCounter
    return imageCache[path] ?? null
  }

  function getVideoUrl(path: string): string | null {
    const entry = mediaEntries.get(path)
    if (entry && videoCache[path] === entry.url) entry.lastUsed = ++mediaUseCounter
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
    const pending = pendingReadRequests.value.get(requestId)
    // Unsolicited/late results have no owned request and must not release a
    // concurrency slot that may now belong to another request.
    if (!pending) return
    // Error first: a failed final must clear fetching flags and the
    // concurrency slot (including the request safety timer) before any
    // other handling.
    if (error) {
      pendingReadRequests.value.delete(requestId)
      clearFetchTimeout(requestId)
      getMediaChunks().cancel(requestId)
      failPendingMedia(pending.path, pending.kind)
      const notify = useNotificationStore()
      notify.error('Media load failed', error)
      return
    }
    if (options.chunked) {
      try {
        const assembled = getMediaChunks().finish(requestId, options.totalChunks)
        const expectedPath = pending.path
        if (path !== expectedPath || (assembled.path && assembled.path !== expectedPath)) {
          throw new Error(`path mismatch for ${requestId}`)
        }
        pendingReadRequests.value.delete(requestId)
        clearFetchTimeout(requestId)
        storeAssembledMedia(expectedPath, pending.kind, assembled.content, mimeType)
      } catch {
        pendingReadRequests.value.delete(requestId)
        clearFetchTimeout(requestId)
        getMediaChunks().cancel(requestId)
        failPendingMedia(pending.path, pending.kind)
      }
      return
    }

    pendingReadRequests.value.delete(requestId)
    clearFetchTimeout(requestId)

    if (!content) {
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
    const pending = pendingReadRequests.value.get(requestId)
    if (!pending) return
    if (pending.path !== path) {
      pendingReadRequests.value.delete(requestId)
      clearFetchTimeout(requestId)
      getMediaChunks().cancel(requestId)
      failPendingMedia(pending.path, pending.kind)
      return
    }
    try {
      const store = getMediaChunks()
      if (!store.has(requestId)) {
        store.start(requestId, totalChunks, { totalChunks }, pending.path)
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
      failPendingMedia(pending.path, pending.kind)
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
        const dataUrl = `data:${mime};base64,${cleanBase64}`
        imageCache[path] = dataUrl
        trackMedia(path, 'image', dataUrl, estimateDataUrlBytes(dataUrl))
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

        const url = URL.createObjectURL(new Blob([bytes], { type: resolvedMime }))
        videoCache[path] = url
        trackMedia(path, 'video', url, bytes.byteLength)
        videoMimeTypes[path] = resolvedMime
        delete fetchingVideos[path]
      }
    } catch {
      failPendingMedia(path, kind)
      const notify = useNotificationStore()
      notify.error('Media load failed', 'The media payload could not be decoded.')
      return
    }

    trimMediaCache()
    activeCount = Math.max(0, activeCount - 1)
    processQueue()
  }

  /**
   * Directly store a local data URL for an image path (used for optimistic preview
   * after a local file is dropped/uploaded before the runner confirms).
   */
  function storeLocalImage(path: string, dataUrl: string): void {
    imageCache[path] = dataUrl
    trackMedia(path, 'image', dataUrl, estimateDataUrlBytes(dataUrl))
    delete fetchingPaths[path]
    if (failedUploadPaths.has(path)) {
      failedUploadPaths.delete(path)
      if (!uploadPinnedPaths.has(path)) {
        uploadPinnedPaths.add(path)
        pinMedia(path, true)
      }
    } else if (uploadStatuses[path] === 'uploading' && !uploadPinnedPaths.has(path)) {
      uploadPinnedPaths.add(path)
      pinMedia(path, true)
    }
    trimMediaCache()
  }

  /** Register an in-flight upload so the UI can show upload feedback. */
  function trackUpload(requestId: string, path: string): void {
    const previousTimeout = uploadStatusTimeouts.get(path)
    if (previousTimeout !== undefined) clearTimeout(previousTimeout)
    uploadStatusTimeouts.delete(path)
    pendingUploadIds.value.set(requestId, path)
    uploadStatuses[path] = 'uploading'
    failedUploadPaths.delete(path)
    if (mediaUrl(path) && !uploadPinnedPaths.has(path)) {
      uploadPinnedPaths.add(path)
      pinMedia(path, true)
    }
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
      const timeout = setTimeout(() => {
        uploadStatusTimeouts.delete(path)
        if (uploadStatuses[path] === 'done') delete uploadStatuses[path]
      }, 3000)
      uploadStatusTimeouts.set(path, timeout)
      if (uploadPinnedPaths.delete(path)) pinMedia(path, false)
      trimMediaCache()
    } else {
      const previousTimeout = uploadStatusTimeouts.get(path)
      if (previousTimeout !== undefined) clearTimeout(previousTimeout)
      uploadStatusTimeouts.delete(path)
      uploadStatuses[path] = 'error'
      removeMedia(path)
      if ((mediaPins.get(path) ?? 0) > 0) failedUploadPaths.add(path)
      if (uploadPinnedPaths.delete(path)) pinMedia(path, false)
    }
  }

  function getUploadStatus(path: string): UploadStatus | null {
    return uploadStatuses[path] ?? null
  }

  function reset(): void {
    const urlsToRevoke = Object.values(videoCache)
    for (const urls of retiredVideoUrls.values()) urlsToRevoke.push(...urls)
    Object.keys(imageCache).forEach((k) => {
      delete imageCache[k]
    })
    Object.keys(videoCache).forEach((k) => {
      delete videoCache[k]
    })
    Object.keys(videoMimeTypes).forEach((k) => {
      delete videoMimeTypes[k]
    })
    Object.keys(fetchingPaths).forEach((k) => {
      delete fetchingPaths[k]
    })
    Object.keys(fetchingVideos).forEach((k) => {
      delete fetchingVideos[k]
    })
    Object.keys(uploadStatuses).forEach((k) => {
      delete uploadStatuses[k]
    })
    fetchTimeouts.forEach((t) => clearTimeout(t))
    fetchTimeouts.clear()
    uploadStatusTimeouts.forEach((t) => clearTimeout(t))
    uploadStatusTimeouts.clear()
    mediaEntries.clear()
    mediaUseCounter = 0
    mediaPins.clear()
    retiredVideoUrls.clear()
    failedUploadPaths.clear()
    uploadPinnedPaths.clear()
    pendingReadRequests.value.clear()
    pendingUploadIds.value.clear()
    mediaChunks?.clear()
    fetchQueue.length = 0
    activeCount = 0
    mediaGeneration.value += 1
    void nextTick(() => {
      for (const url of urlsToRevoke) URL.revokeObjectURL(url)
      trimMediaCache()
    })
  }

  return {
    imageCache,
    videoCache,
    videoMimeTypes,
    fetchingPaths,
    fetchingVideos,
    uploadStatuses,
    mediaGeneration,
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
    pinMedia,
    releaseMedia,
    trackUpload,
    handleUploadResult,
    getUploadStatus,
    handleContentResult,
    handleContentChunk,
    reset,
  }
})
