/**
 * Workspace media cache store — fetches media from the workspace filesystem
 * via the runner's file-read WebSocket channel and caches images/videos for chat rendering.
 */

import { defineStore } from 'pinia'
import { ref, reactive } from 'vue'
import { sendFilesRead } from '@/services/socket'

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

  // paths currently being fetched
  const fetchingPaths = reactive<Record<string, boolean>>({})
  const fetchingVideos = reactive<Record<string, boolean>>({})

  // Timeout handles for video fetch requests (to recover from silent failures)
  const videoFetchTimeouts = new Map<string, ReturnType<typeof setTimeout>>()

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
      sendFilesRead(workspaceId, requestId, path)
    } else {
      fetchQueue.push({ workspaceId, path, kind: 'image' })
    }
  }

  function fetchVideo(workspaceId: string, path: string): void {
    if (videoCache[path] || fetchingVideos[path]) return
    fetchingVideos[path] = true

    // Safety timeout: if the backend never responds (runner offline without
    // sending an error, or connection dropped mid-transfer), reset the
    // fetching state after 30 s so the user can retry.
    const timeoutHandle = setTimeout(() => {
      if (fetchingVideos[path]) {
        delete fetchingVideos[path]
        videoFetchTimeouts.delete(path)
        // Also clean up any pending request tracking
        for (const [reqId, req] of pendingReadRequests.value) {
          if (req.path === path && req.kind === 'video') {
            pendingReadRequests.value.delete(reqId)
            activeCount = Math.max(0, activeCount - 1)
            break
          }
        }
        processQueue()
      }
    }, 30_000)
    videoFetchTimeouts.set(path, timeoutHandle)

    if (activeCount < MAX_CONCURRENT) {
      activeCount++
      const requestId = nextRequestId()
      pendingReadRequests.value.set(requestId, { path, kind: 'video' })
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
   * Called from WorkspaceDetailView when a files:content_result event arrives.
   * Only processes requests initiated by this store.
   */
  function handleContentResult(
    requestId: string,
    path: string,
    content: string,
    error?: string,
    mimeType?: string,
  ): void {
    const pending = pendingReadRequests.value.get(requestId)
    if (!pending) return
    pendingReadRequests.value.delete(requestId)

    if (pending.kind === 'video') {
      const t = videoFetchTimeouts.get(path)
      if (t !== undefined) {
        clearTimeout(t)
        videoFetchTimeouts.delete(path)
      }
    }

    if (error || !content) {
      if (pending.kind === 'image') delete fetchingPaths[path]
      else delete fetchingVideos[path]
      activeCount--
      processQueue()
      return
    }

    const cleanBase64 = content.replace(/\s+/g, '')

    if (pending.kind === 'image') {
      const ext = getExt(path)
      const mime = mimeType || IMAGE_MIME_MAP[ext] || 'image/png'
      imageCache[path] = `data:${mime};base64,${cleanBase64}`
      delete fetchingPaths[path]
    } else {
      const ext = getExt(path)
      const resolvedMime = mimeType || VIDEO_MIME_MAP[ext] || 'video/mp4'
      const binary = atob(cleanBase64)
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

    activeCount--
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
   * Called from WorkspaceDetailView when a files:upload_result event arrives.
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
    videoFetchTimeouts.forEach((t) => clearTimeout(t))
    videoFetchTimeouts.clear()
    pendingReadRequests.value.clear()
    pendingUploadIds.value.clear()
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
    reset,
  }
})
