/**
 * Workspace media cache store — fetches media from the workspace filesystem
 * via the runner's file-read WebSocket channel and caches images/videos for chat rendering.
 */

import { defineStore } from 'pinia'
import { nextTick, ref, reactive } from 'vue'
import { readWorkspaceFile } from '@/services/socket'
import { useNotificationStore } from '@/stores/notifications'

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

  const fetchingPaths = reactive<Record<string, boolean>>({})
  const fetchingVideos = reactive<Record<string, boolean>>({})
  let readController = new AbortController()

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
      void readMedia(item, mediaGeneration.value, readController.signal)
    }
  }

  async function readMedia(
    item: (typeof fetchQueue)[number],
    generation: number,
    signal: AbortSignal,
  ): Promise<void> {
    try {
      const result = await readWorkspaceFile(item.workspaceId, item.path, item.maxSize, signal)
      if (generation !== mediaGeneration.value) return
      storeAssembledMedia(item.path, item.kind, result.content, result.mime_type)
    } catch (error) {
      if (generation !== mediaGeneration.value) return
      useNotificationStore().error(
        'Media load failed',
        error instanceof Error ? error.message : 'The media could not be loaded.',
      )
    } finally {
      if (generation === mediaGeneration.value) {
        if (item.kind === 'image') delete fetchingPaths[item.path]
        else delete fetchingVideos[item.path]
        activeCount--
        processQueue()
      }
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
    fetchQueue.push({ workspaceId, path, kind: 'image' })
    processQueue()
  }

  function fetchVideo(workspaceId: string, path: string): void {
    if (videoCache[path] || fetchingVideos[path]) return
    fetchingVideos[path] = true
    fetchQueue.push({ workspaceId, path, kind: 'video', maxSize: VIDEO_READ_MAX_SIZE })
    processQueue()
  }

  function storeAssembledMedia(
    path: string,
    kind: 'image' | 'video',
    content: string,
    mimeType?: string,
  ): void {
    const cleanBase64 = content.replace(/\s+/g, '')
    if (!cleanBase64) return

    if (kind === 'image') {
      const mime = mimeType || IMAGE_MIME_MAP[getExt(path)] || 'image/png'
      const dataUrl = `data:${mime};base64,${cleanBase64}`
      imageCache[path] = dataUrl
      trackMedia(path, 'image', dataUrl, estimateDataUrlBytes(dataUrl))
    } else {
      const resolvedMime = mimeType || VIDEO_MIME_MAP[getExt(path)] || 'video/mp4'
      const binary = atob(cleanBase64)
      const bytes = new Uint8Array(binary.length)
      for (let index = 0; index < binary.length; index++) {
        bytes[index] = binary.charCodeAt(index)
      }
      const url = URL.createObjectURL(new Blob([bytes], { type: resolvedMime }))
      videoCache[path] = url
      trackMedia(path, 'video', url, bytes.byteLength)
      videoMimeTypes[path] = resolvedMime
    }
    trimMediaCache()
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
    readController.abort()
    readController = new AbortController()
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
    uploadStatusTimeouts.forEach((t) => clearTimeout(t))
    uploadStatusTimeouts.clear()
    mediaEntries.clear()
    mediaUseCounter = 0
    mediaPins.clear()
    retiredVideoUrls.clear()
    failedUploadPaths.clear()
    uploadPinnedPaths.clear()
    pendingUploadIds.value.clear()
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
    reset,
  }
})
