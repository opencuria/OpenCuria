<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { Download, Minus, Plus, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import VoiceCaptureBar from '@/components/chat/VoiceCaptureBar.vue'
import { voiceCaptureControls } from '@/lib/voiceCaptureControls'
import { activePointingWorkspace } from '@/lib/voicePointing'
import { useNotificationStore } from '@/stores/notifications'

const props = defineProps<{
  src: string
  alt?: string
  /** Original workspace reference, independent of the temporary browser URL. */
  path?: string
  workspaceId?: string
}>()

const emit = defineEmits<{
  close: []
}>()

interface Point {
  x: number
  y: number
}

const triggerEl = document.activeElement instanceof HTMLElement ? document.activeElement : null
const stageEl = ref<HTMLElement | null>(null)
const loadFailed = ref(false)
const isDownloading = ref(false)
// Zoom is relative to the original pixels: 100% always means actual image size.
const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)
const isDragging = ref(false)
const naturalWidth = ref(0)
const naturalHeight = ref(0)
const stageWidth = ref(0)
const stageHeight = ref(0)

const maxZoom = 8
const zoomLevels = [0.1, 0.125, 1 / 6, 0.25, 1 / 3, 0.5, 2 / 3, 0.75, 1, 1.25, 1.5, 2, 3, 4, 6, 8]
const pointers = new Map<number, Point>()
let resizeObserver: ResizeObserver | null = null
let downloadController: AbortController | null = null

const imageReady = computed(() => !loadFailed.value && naturalWidth.value > 0)
const minZoom = computed(() => {
  if (!naturalWidth.value || !naturalHeight.value || !stageWidth.value || !stageHeight.value)
    return 1
  return Math.min(1, stageWidth.value / naturalWidth.value, stageHeight.value / naturalHeight.value)
})
const maxPanX = computed(() =>
  Math.max(0, (naturalWidth.value * zoom.value - stageWidth.value) / 2),
)
const maxPanY = computed(() =>
  Math.max(0, (naturalHeight.value * zoom.value - stageHeight.value) / 2),
)
const canPan = computed(() => maxPanX.value > 0 || maxPanY.value > 0)
const zoomPercent = computed(() => `${Math.round(zoom.value * 100)}%`)

function restoreFocus(event: Event): void {
  event.preventDefault()
  if (triggerEl?.isConnected) triggerEl.focus()
}

function keepViewerFocus(): void {
  // Disabling a zoom control at its limit can drop browser focus to the page.
  void nextTick(() => {
    const dialog = stageEl.value?.closest<HTMLElement>('[role="dialog"]')
    if (dialog && document.activeElement === document.body) dialog.focus()
  })
}

function clampPan(nextX = panX.value, nextY = panY.value): void {
  panX.value = Math.min(maxPanX.value, Math.max(-maxPanX.value, nextX))
  panY.value = Math.min(maxPanY.value, Math.max(-maxPanY.value, nextY))
}

function resetView(): void {
  zoom.value = minZoom.value
  panX.value = 0
  panY.value = 0
}

function syncStageSize(): void {
  const stage = stageEl.value
  if (!stage) return
  const wasFitted = Math.abs(zoom.value - minZoom.value) < 0.0001
  // Layout dimensions stay accurate during the dialog's opening animation.
  stageWidth.value = stage.clientWidth
  stageHeight.value = stage.clientHeight
  if (wasFitted) resetView()
  else setZoom(zoom.value)
}

function setZoom(nextZoom: number, origin?: Point, nextOrigin = origin): void {
  const clampedZoom = Math.min(maxZoom, Math.max(minZoom.value, nextZoom))
  const ratio = clampedZoom / zoom.value
  const center = { x: stageWidth.value / 2, y: stageHeight.value / 2 }
  const from = origin ?? center
  const to = nextOrigin ?? from
  // Preserve the image pixel under the cursor (or moving pinch midpoint).
  const nextX = to.x - center.x - (from.x - center.x - panX.value) * ratio
  const nextY = to.y - center.y - (from.y - center.y - panY.value) * ratio
  zoom.value = clampedZoom
  clampPan(nextX, nextY)
}

function zoomIn(): void {
  setZoom(zoomLevels.find((level) => level > zoom.value + 0.0001) ?? maxZoom)
}

function zoomOut(): void {
  setZoom([...zoomLevels].reverse().find((level) => level < zoom.value - 0.0001) ?? minZoom.value)
}

function onImageLoad(event: Event): void {
  const img = event.target as HTMLImageElement
  naturalWidth.value = img.naturalWidth
  naturalHeight.value = img.naturalHeight
  syncStageSize()
  resetView()
}

function getStagePoint(clientX: number, clientY: number): Point {
  const rect = stageEl.value!.getBoundingClientRect()
  return { x: clientX - rect.left, y: clientY - rect.top }
}

function onKeydown(event: KeyboardEvent): void {
  if (!imageReady.value || event.ctrlKey || event.metaKey || event.altKey) return
  if (!['+', '=', '-', '0', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key))
    return
  event.preventDefault()
  if (event.key === '+' || event.key === '=') zoomIn()
  if (event.key === '-') zoomOut()
  if (event.key === '0') resetView()
  const step = event.shiftKey ? 150 : 50
  if (event.key === 'ArrowLeft') clampPan(panX.value + step, panY.value)
  if (event.key === 'ArrowRight') clampPan(panX.value - step, panY.value)
  if (event.key === 'ArrowUp') clampPan(panX.value, panY.value + step)
  if (event.key === 'ArrowDown') clampPan(panX.value, panY.value - step)
}

function onWheel(event: WheelEvent): void {
  if (!imageReady.value) return
  // Browsers report wheel distances in pixels, lines, or pages. Tiny trackpad
  // deltas must stay tiny instead of triggering a full button-sized zoom step.
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? stageHeight.value : 1
  const delta = Math.max(-100, Math.min(100, event.deltaY * unit))
  const sensitivity = event.ctrlKey ? 0.005 : 0.001
  setZoom(zoom.value * Math.exp(-delta * sensitivity), getStagePoint(event.clientX, event.clientY))
}

function onPointerDown(event: PointerEvent): void {
  if (!imageReady.value || event.button !== 0 || pointers.size >= 2) return
  event.preventDefault()
  pointers.set(event.pointerId, getStagePoint(event.clientX, event.clientY))
  isDragging.value = true
  stageEl.value?.setPointerCapture(event.pointerId)
}

function onPointerMove(event: PointerEvent): void {
  const previous = pointers.get(event.pointerId)
  if (!previous) return
  const point = getStagePoint(event.clientX, event.clientY)
  const other = [...pointers.entries()].find(([id]) => id !== event.pointerId)?.[1]
  if (other) {
    const distance = Math.hypot(previous.x - other.x, previous.y - other.y)
    if (distance > 0) {
      setZoom(
        (zoom.value * Math.hypot(point.x - other.x, point.y - other.y)) / distance,
        { x: (previous.x + other.x) / 2, y: (previous.y + other.y) / 2 },
        { x: (point.x + other.x) / 2, y: (point.y + other.y) / 2 },
      )
    }
  } else {
    clampPan(panX.value + point.x - previous.x, panY.value + point.y - previous.y)
  }
  pointers.set(event.pointerId, point)
}

function onPointerEnd(event: PointerEvent): void {
  pointers.delete(event.pointerId)
  isDragging.value = pointers.size > 0
  if (stageEl.value?.hasPointerCapture(event.pointerId)) {
    stageEl.value.releasePointerCapture(event.pointerId)
  }
}

function clearPointers(): void {
  for (const id of pointers.keys()) {
    if (stageEl.value?.hasPointerCapture(id)) stageEl.value.releasePointerCapture(id)
  }
  pointers.clear()
  isDragging.value = false
}

function onDoubleClick(event: MouseEvent): void {
  if (!imageReady.value) return
  if (zoom.value > minZoom.value + 0.0001) resetView()
  else setZoom(Math.max(1, minZoom.value * 2), getStagePoint(event.clientX, event.clientY))
}

function downloadFilename(mime: string): string {
  const pathName = props.path?.split('/').pop()
  if (pathName) return pathName
  const source = new URL(props.src, window.location.href)
  if (source.protocol === 'http:' || source.protocol === 'https:') {
    const name = decodeURIComponent(source.pathname.split('/').pop() ?? '')
    if (name.includes('.')) return name
  }
  const extension = mime === 'image/jpeg' ? 'jpg' : mime.split('/')[1]?.split('+')[0] || 'png'
  return `image.${extension}`
}

async function downloadImage(): Promise<void> {
  if (isDownloading.value || !imageReady.value) return
  const controller = new AbortController()
  downloadController = controller
  isDownloading.value = true
  try {
    const response = await fetch(props.src, { signal: controller.signal })
    if (!response.ok) throw new Error('Image download failed')
    const blob = await response.blob()
    if (controller.signal.aborted) return
    const filename = downloadFilename(blob.type)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    try {
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
    } finally {
      anchor.remove()
      // Allow the browser to start reading the blob before revoking it.
      setTimeout(() => URL.revokeObjectURL(url), 30_000)
    }
  } catch {
    if (!controller.signal.aborted) {
      useNotificationStore().error(
        'Download failed',
        'The image could not be downloaded. Please retry.',
      )
    }
  } finally {
    if (downloadController === controller) {
      downloadController = null
      isDownloading.value = false
    }
  }
}

watch(
  () => props.src,
  () => {
    downloadController?.abort()
    downloadController = null
    isDownloading.value = false
    clearPointers()
    loadFailed.value = false
    naturalWidth.value = 0
    naturalHeight.value = 0
    resetView()
  },
)

// Dialog content mounts through a portal after the lightbox itself.
watch(
  stageEl,
  (stage) => {
    resizeObserver?.disconnect()
    if (!stage) return
    syncStageSize()
    resizeObserver = new ResizeObserver(syncStageSize)
    resizeObserver.observe(stage)
  },
  { flush: 'post' },
)

onUnmounted(() => {
  resizeObserver?.disconnect()
  downloadController?.abort()
  clearPointers()
})
</script>

<template>
  <Dialog :open="true" @update:open="!$event && emit('close')">
    <DialogContent
      :show-close-button="false"
      aria-modal="true"
      class="top-0 left-0 h-[100dvh] w-screen max-w-none max-h-none translate-x-0 translate-y-0 gap-0 rounded-none bg-background/95 p-0 text-foreground ring-0 sm:max-w-none"
      @close-auto-focus="restoreFocus"
      @keydown="onKeydown"
      @focusout="keepViewerFocus"
    >
      <DialogTitle class="sr-only">Image Viewer</DialogTitle>
      <DialogDescription class="sr-only">
        Scroll or pinch to zoom, drag to pan, double-click to toggle zoom. Press 0 to fit or Escape
        to close.
      </DialogDescription>
      <div
        v-if="voiceCaptureControls"
        class="absolute inset-x-4 bottom-4 z-20 mx-auto w-fit max-w-[calc(100vw-2rem)]"
      >
        <p
          v-if="activePointingWorkspace && (!props.path || !props.workspaceId)"
          role="status"
          class="mb-2 rounded-md border bg-card px-3 py-2 text-xs text-muted-foreground"
        >
          This image has no workspace path. Pointing cannot be linked to it.
        </p>
        <VoiceCaptureBar class="w-[min(36rem,calc(100vw-2rem))] shadow-md" />
      </div>
      <div
        class="absolute top-3 right-3 z-10 flex items-center gap-1 rounded-full border bg-card p-1 shadow-sm sm:top-4 sm:right-4"
      >
        <Button
          variant="ghost"
          size="icon"
          class="rounded-full"
          aria-label="Zoom out"
          title="Zoom out (-)"
          :disabled="zoom <= minZoom || !imageReady"
          @click="zoomOut"
        >
          <Minus />
        </Button>
        <span aria-label="Zoom level" class="min-w-14 text-center text-sm font-medium tabular-nums">
          {{ zoomPercent }}
        </span>
        <Button
          variant="ghost"
          size="icon"
          class="rounded-full"
          aria-label="Zoom in"
          title="Zoom in (+)"
          :disabled="zoom >= maxZoom || !imageReady"
          @click="zoomIn"
        >
          <Plus />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          class="rounded-full"
          aria-label="Download image"
          title="Download image"
          :disabled="!imageReady || isDownloading"
          :aria-busy="isDownloading"
          @click="downloadImage"
        >
          <Download />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          class="rounded-full"
          aria-label="Close image preview"
          title="Close (Esc)"
          @click="emit('close')"
        >
          <X />
        </Button>
      </div>

      <div
        ref="stageEl"
        data-slot="image-viewport"
        class="absolute inset-x-3 top-16 bottom-3 touch-none overflow-hidden overscroll-contain sm:inset-x-4 sm:top-18 sm:bottom-4"
        :class="isDragging ? 'cursor-grabbing' : canPan ? 'cursor-grab' : 'cursor-default'"
        @wheel.prevent="onWheel"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerEnd"
        @pointercancel="onPointerEnd"
        @lostpointercapture="onPointerEnd"
        @dblclick.prevent="onDoubleClick"
      >
        <div class="flex h-full items-center justify-center">
          <p v-if="loadFailed" role="status" class="text-sm text-muted-foreground">
            Image could not be loaded.
          </p>
          <img
            v-else
            :src="props.src"
            :alt="props.alt ?? 'Image'"
            :data-voice-image-path="props.path"
            :data-voice-workspace="props.workspaceId"
            :style="{
              width: `${naturalWidth}px`,
              height: `${naturalHeight}px`,
              transform: `translate3d(${panX}px, ${panY}px, 0) scale(${zoom})`,
            }"
            class="max-w-none shrink-0 select-none shadow-2xl"
            draggable="false"
            @load="onImageLoad"
            @error="loadFailed = true"
          />
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
