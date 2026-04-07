<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { X, ZoomIn, ZoomOut, RotateCcw, Move, Maximize2 } from 'lucide-vue-next'

const props = defineProps<{
  src: string
  alt?: string
}>()

const emit = defineEmits<{
  close: []
}>()

const stageEl = ref<HTMLElement | null>(null)
const zoom = ref(1)
const panX = ref(0)
const panY = ref(0)
const isDragging = ref(false)
const naturalWidth = ref(0)
const naturalHeight = ref(0)
const stageWidth = ref(0)
const stageHeight = ref(0)

const minZoom = 1
const maxZoom = 6
const zoomStep = 1.2

let activePointerId: number | null = null
let dragStartX = 0
let dragStartY = 0
let dragOriginX = 0
let dragOriginY = 0
let resizeObserver: ResizeObserver | null = null

const fittedScale = computed(() => {
  if (!naturalWidth.value || !naturalHeight.value || !stageWidth.value || !stageHeight.value) return 1
  return Math.min(stageWidth.value / naturalWidth.value, stageHeight.value / naturalHeight.value)
})

const fittedWidth = computed(() => naturalWidth.value * fittedScale.value)
const fittedHeight = computed(() => naturalHeight.value * fittedScale.value)

const maxPanX = computed(() => Math.max(0, (fittedWidth.value * zoom.value - stageWidth.value) / 2))
const maxPanY = computed(() => Math.max(0, (fittedHeight.value * zoom.value - stageHeight.value) / 2))

const zoomPercent = computed(() => `${Math.round(zoom.value * 100)}%`)

function clampPan(nextX = panX.value, nextY = panY.value): void {
  panX.value = Math.min(maxPanX.value, Math.max(-maxPanX.value, nextX))
  panY.value = Math.min(maxPanY.value, Math.max(-maxPanY.value, nextY))
}

function syncStageSize(): void {
  const rect = stageEl.value?.getBoundingClientRect()
  if (!rect) return
  stageWidth.value = rect.width
  stageHeight.value = rect.height
  clampPan()
}

function resetView(): void {
  zoom.value = 1
  panX.value = 0
  panY.value = 0
}

function setZoom(nextZoom: number, origin?: { x: number; y: number }): void {
  const clampedZoom = Math.min(maxZoom, Math.max(minZoom, nextZoom))
  if (clampedZoom === zoom.value) return

  if (!origin || !stageWidth.value || !stageHeight.value) {
    zoom.value = clampedZoom
    if (clampedZoom === minZoom) {
      panX.value = 0
      panY.value = 0
    } else {
      clampPan()
    }
    return
  }

  const stageCenterX = stageWidth.value / 2
  const stageCenterY = stageHeight.value / 2
  const contentX = (origin.x - stageCenterX - panX.value) / zoom.value
  const contentY = (origin.y - stageCenterY - panY.value) / zoom.value

  zoom.value = clampedZoom
  panX.value = origin.x - stageCenterX - contentX * clampedZoom
  panY.value = origin.y - stageCenterY - contentY * clampedZoom

  if (clampedZoom === minZoom) {
    panX.value = 0
    panY.value = 0
  } else {
    clampPan()
  }
}

function zoomIn(origin?: { x: number; y: number }): void {
  setZoom(zoom.value * zoomStep, origin)
}

function zoomOut(origin?: { x: number; y: number }): void {
  setZoom(zoom.value / zoomStep, origin)
}

function onImageLoad(event: Event): void {
  const img = event.target as HTMLImageElement
  naturalWidth.value = img.naturalWidth
  naturalHeight.value = img.naturalHeight
  syncStageSize()
}

function getRelativeStagePoint(clientX: number, clientY: number): { x: number; y: number } | null {
  const rect = stageEl.value?.getBoundingClientRect()
  if (!rect) return null
  return {
    x: clientX - rect.left,
    y: clientY - rect.top,
  }
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') emit('close')
  if (event.key === '+' || event.key === '=') zoomIn()
  if (event.key === '-') zoomOut()
  if (event.key === '0') resetView()
}

function onWheel(event: WheelEvent): void {
  const origin = getRelativeStagePoint(event.clientX, event.clientY)
  if (event.deltaY < 0) zoomIn(origin ?? undefined)
  else zoomOut(origin ?? undefined)
}

function onPointerDown(event: PointerEvent): void {
  if (zoom.value <= minZoom) return
  activePointerId = event.pointerId
  isDragging.value = true
  dragStartX = event.clientX
  dragStartY = event.clientY
  dragOriginX = panX.value
  dragOriginY = panY.value
  stageEl.value?.setPointerCapture(event.pointerId)
}

function onPointerMove(event: PointerEvent): void {
  if (!isDragging.value || activePointerId !== event.pointerId) return
  clampPan(
    dragOriginX + (event.clientX - dragStartX),
    dragOriginY + (event.clientY - dragStartY),
  )
}

function stopDragging(event?: PointerEvent): void {
  if (event && activePointerId === event.pointerId) {
    stageEl.value?.releasePointerCapture(event.pointerId)
  }
  activePointerId = null
  isDragging.value = false
}

function onDoubleClick(event: MouseEvent): void {
  const origin = getRelativeStagePoint(event.clientX, event.clientY)
  if (zoom.value > 1.5) resetView()
  else zoomIn(origin ?? undefined)
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  syncStageSize()
  resizeObserver = new ResizeObserver(() => syncStageSize())
  if (stageEl.value) resizeObserver.observe(stageEl.value)
})

onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  resizeObserver?.disconnect()
})
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[9999] bg-[radial-gradient(circle_at_top,rgba(255,255,255,0.08),transparent_28%),linear-gradient(180deg,rgba(9,10,12,0.94),rgba(5,6,8,0.98))] text-white"
      @click.self="emit('close')"
    >
      <div class="absolute inset-x-0 top-0 z-10 flex items-start justify-end gap-4 px-4 py-4 sm:justify-between sm:px-6">
        <div class="hidden max-w-[min(50rem,72vw)] sm:block">
          <p class="text-[11px] font-semibold uppercase tracking-[0.24em] text-white/45">Image Viewer</p>
          <p v-if="props.alt" class="mt-1 truncate text-sm text-white/88">{{ props.alt }}</p>
          <p v-else class="mt-1 text-sm text-white/60">Full-screen preview with zoom and pan.</p>
        </div>

        <div class="flex items-center gap-2 rounded-full border border-white/10 bg-black/35 px-2 py-2 backdrop-blur-md">
          <button
            class="flex h-10 w-10 items-center justify-center rounded-full bg-white/8 text-white/85 transition-colors hover:bg-white/16 hover:text-white"
            title="Zoom out (-)"
            @click="zoomOut()"
          >
            <ZoomOut :size="18" />
          </button>
          <div class="min-w-[4.75rem] text-center text-sm font-medium tabular-nums text-white/88">
            {{ zoomPercent }}
          </div>
          <button
            class="flex h-10 w-10 items-center justify-center rounded-full bg-white/8 text-white/85 transition-colors hover:bg-white/16 hover:text-white"
            title="Zoom in (+)"
            @click="zoomIn()"
          >
            <ZoomIn :size="18" />
          </button>
          <button
            class="flex h-10 w-10 items-center justify-center rounded-full bg-white/8 text-white/85 transition-colors hover:bg-white/16 hover:text-white"
            title="Reset view (0)"
            @click="resetView"
          >
            <RotateCcw :size="18" />
          </button>
          <button
            class="ml-1 flex h-10 w-10 items-center justify-center rounded-full bg-white/8 text-white/85 transition-colors hover:bg-white/16 hover:text-white"
            title="Close (Esc)"
            @click="emit('close')"
          >
            <X :size="18" />
          </button>
        </div>
      </div>

      <div
        ref="stageEl"
        class="absolute inset-x-0 top-[5rem] bottom-[4.5rem] overflow-hidden px-3 sm:top-[5.75rem] sm:px-6"
        :class="zoom > 1 ? (isDragging ? 'cursor-grabbing' : 'cursor-grab') : 'cursor-zoom-in'"
        @wheel.prevent="onWheel"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="stopDragging"
        @pointercancel="stopDragging"
        @pointerleave="stopDragging"
        @dblclick.prevent="onDoubleClick"
        @click="emit('close')"
      >
        <div class="flex h-full items-center justify-center">
          <img
            :src="props.src"
            :alt="props.alt ?? 'Image'"
            :style="{
              width: `${fittedWidth}px`,
              height: `${fittedHeight}px`,
              transform: `translate3d(${panX}px, ${panY}px, 0) scale(${zoom})`,
              transition: isDragging ? 'none' : 'transform 0.16s ease-out',
            }"
            class="max-w-none select-none object-contain shadow-[0_32px_90px_rgba(0,0,0,0.45)]"
            draggable="false"
            @load="onImageLoad"
            @click.stop
          />
        </div>
      </div>

      <div class="absolute inset-x-0 bottom-0 z-10 flex items-center justify-between gap-4 px-4 py-3 text-xs text-white/55 sm:px-6">
        <div class="flex items-center gap-2">
          <Move :size="14" />
          <span>Drag to pan when zoomed</span>
        </div>
        <div class="flex items-center gap-2">
          <Maximize2 :size="14" />
          <span>Wheel or double-click to zoom</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>
