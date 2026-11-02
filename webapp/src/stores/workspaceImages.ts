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
const IMAGE_READ_MAX_SIZE = 20 * 1024 * 1024 // 20 MiB, as on mobile

/** Maximum retained media payload size, excluding protected visible media. */
export const MEDIA_CACHE_BUDGET_BYTES = 32 * 1024 * 1024

/** Base64 strings are retained as UTF-16 JavaScript strings. */
const BASE64_STORAGE_BYTES_PER_CHAR = 2

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
  let mediaUseCounter = 0

  function estimateDataUrlBytes(url: string): number {
    return url.length * BASE64_STORAGE_BYTES_PER_CHAR
  }

  function trackMedia(path: string, kind: 'image' | 'video', url: string, bytes: number): void {
    mediaEntries.set(path, {
      kind,
      url,
      bytes,
      lastUsed: ++mediaUseCounter,
    })
  }

  function trimMediaCache(): void {
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
  }

  const fetchingPaths = reactive<Record<string, boolean>>({})
  const fetchingVideos = reactive<Record<string, boolean>>({})
  let readController = new AbortController()

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
      if (result.truncated) throw new Error('The media file is too large to preview.')
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
    fetchQueue.push({ workspaceId, path, kind: 'image', maxSize: IMAGE_READ_MAX_SIZE })
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

  function reset(): void {
    readController.abort()
    readController = new AbortController()
    const urlsToRevoke = Object.values(videoCache)
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
    mediaEntries.clear()
    mediaUseCounter = 0
    mediaPins.clear()
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
    pinMedia,
    releaseMedia,
    reset,
  }
})
