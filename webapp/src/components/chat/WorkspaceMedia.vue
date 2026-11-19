<script setup lang="ts">
/**
 * One workspace image or video in a chat, streamed from its signed link.
 * Small files load by themselves. Larger ones show the blurred thumbnail with
 * their size and load on tap (remembered per workspace for the session);
 * files above the transfer limit only show the preview. Loaded images open
 * the lightbox. Unavailable files render the `fallback` slot.
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { Download, Play } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import ImageLightbox from './ImageLightbox.vue'
import { formatAttachmentSize } from '@opencuria/chat-core/harnessAttachments'
import {
  FILE_TRANSFER_MAX_BYTES,
  MEBIBYTE,
  mediaLoadMode,
  type WorkspaceFileLink,
} from '@opencuria/chat-core/workspaceFiles'
import { workspaceFiles } from '@/services/workspaceFiles'
import { useWorkspaceMediaStore } from '@/stores/workspaceMedia'

const props = withDefaults(
  defineProps<{
    workspaceId: string
    path: string
    kind: 'image' | 'video'
    alt?: string
    /** `inline` in message text, `tile` in the compact thumbnail strip. */
    variant?: 'inline' | 'tile'
    /** Open loaded images in the lightbox (off inside links). */
    zoomable?: boolean
    onPrimary?: boolean
  }>(),
  { alt: '', variant: 'inline', zoomable: true, onPrimary: false },
)

const TOO_LARGE = `Too large to show (max ${FILE_TRANSFER_MAX_BYTES / MEBIBYTE} MB)`

const media = useWorkspaceMediaStore()
const link = ref<WorkspaceFileLink | null>(null)
const failed = ref(false)
const thumbnailFailed = ref(false)
/** Natural thumbnail size: the media's aspect ratio. */
const thumbnailSize = ref<[number, number] | null>(null)
const tapped = ref(false)
const lightboxOpen = ref(false)
let controller: AbortController | null = null
let renewed = false

const label = computed(() => props.alt || props.path)
const tile = computed(() => props.variant === 'tile')
const mode = computed(() =>
  link.value ? mediaLoadMode(link.value, media.isRequested(props.workspaceId, props.path)) : null,
)
const sizeLabel = computed(() => formatAttachmentSize(link.value?.size ?? 0))
/** The placeholder keeps the media's shape within the inline media bounds (28 × 24 rem). */
const placeholderStyle = computed(() => {
  if (tile.value) return undefined
  const [width, height] = thumbnailSize.value ?? (props.kind === 'video' ? [16, 9] : [4, 3])
  return {
    aspectRatio: `${width} / ${height}`,
    width: `min(100%, ${Math.min(28, (24 * width) / height)}rem)`,
  }
})

function resolve(): void {
  controller?.abort()
  controller = null
  link.value = null
  failed.value = false
  thumbnailFailed.value = false
  thumbnailSize.value = null
  if (!props.workspaceId) {
    failed.value = true
    return
  }
  const cached = workspaceFiles().peekLink(props.workspaceId, props.path)
  if (cached) {
    link.value = cached
    return
  }
  const current = new AbortController()
  controller = current
  workspaceFiles()
    .link(props.workspaceId, props.path, current.signal)
    .then(
      (value) => {
        if (controller === current) link.value = value
      },
      () => {
        if (controller === current) failed.value = true
      },
    )
    .finally(() => {
      if (controller === current) controller = null
    })
}

watch(
  () => [props.workspaceId, props.path] as const,
  () => {
    renewed = false
    tapped.value = false
    lightboxOpen.value = false
    resolve()
  },
  { immediate: true },
)

onUnmounted(() => controller?.abort())

/** A link may expire while a message stays open: renew it once, then give up. */
function onMediaError(): void {
  workspaceFiles().forget(props.workspaceId, props.path)
  if (renewed) {
    failed.value = true
    return
  }
  renewed = true
  resolve()
}

function onThumbnailLoad(event: Event): void {
  const image = event.target as HTMLImageElement
  if (image.naturalWidth > 0 && image.naturalHeight > 0)
    thumbnailSize.value = [image.naturalWidth, image.naturalHeight]
}

function load(): void {
  media.request(props.workspaceId, props.path)
  tapped.value = true
}
</script>

<template>
  <Skeleton
    v-if="!link && !failed"
    data-testid="workspace-media-loading"
    :class="tile ? 'h-14 w-full rounded-lg' : 'my-2 h-24 w-full max-w-md'"
  />
  <slot v-else-if="!link || failed" name="fallback" />
  <template v-else-if="mode === 'auto' && kind === 'image'">
    <Button
      v-if="zoomable"
      type="button"
      variant="ghost"
      data-testid="workspace-media-button"
      class="not-prose"
      aria-haspopup="dialog"
      :aria-label="`Open image preview ${label}`"
      :class="
        tile
          ? [
              'flex h-14 w-full overflow-hidden rounded-lg border bg-muted/30 p-0 shadow-sm hover:border-primary/50 hover:bg-muted/30',
              onPrimary ? 'border-primary-foreground/30' : 'border-border',
            ]
          : 'my-2 block h-auto max-w-full cursor-zoom-in overflow-hidden rounded-md border border-border p-0 hover:bg-transparent'
      "
      @click="lightboxOpen = true"
    >
      <img
        :src="link.contentUrl"
        :alt="label"
        :data-path="path"
        :data-voice-image-path="path"
        :data-voice-workspace="workspaceId"
        data-testid="workspace-media-image"
        draggable="false"
        :class="
          tile
            ? 'm-0 h-full w-full cursor-zoom-in object-contain'
            : 'm-0 max-h-96 max-w-full object-contain'
        "
        @error="onMediaError"
      />
    </Button>
    <img
      v-else
      :src="link.contentUrl"
      :alt="label"
      :data-path="path"
      :data-voice-image-path="path"
      :data-voice-workspace="workspaceId"
      data-testid="workspace-media-image"
      class="not-prose"
      :class="
        tile ? 'h-14 w-full rounded-lg object-contain' : 'my-2 max-h-96 max-w-full object-contain'
      "
      @error="onMediaError"
    />
  </template>
  <!-- No poster: the blurred 48 px thumbnail would set a tiny intrinsic size;
       metadata preloading shows the first frame at the video's own ratio. -->
  <video
    v-else-if="mode === 'auto'"
    :src="link.contentUrl"
    :autoplay="tapped"
    controls
    preload="metadata"
    data-testid="workspace-media-video"
    class="not-prose bg-black"
    :class="
      tile
        ? 'h-14 w-full rounded-lg border border-border object-cover'
        : 'my-2 max-h-96 w-full max-w-md rounded-md border border-border'
    "
    @error="onMediaError"
  />
  <div
    v-else
    data-testid="workspace-media-placeholder"
    :data-mode="mode"
    :title="label"
    class="not-prose relative overflow-hidden border bg-muted"
    :class="[
      tile ? 'h-14 w-full rounded-lg' : 'my-2 max-w-full rounded-md',
      onPrimary ? 'border-primary-foreground/30' : 'border-border',
    ]"
    :style="placeholderStyle"
  >
    <img
      v-if="link.thumbnailUrl && !thumbnailFailed"
      :src="link.thumbnailUrl"
      alt=""
      aria-hidden="true"
      draggable="false"
      class="absolute inset-0 m-0 size-full scale-110 object-cover blur-md"
      @load="onThumbnailLoad"
      @error="thumbnailFailed = true"
    />
    <div class="absolute inset-0 flex flex-col items-center justify-center gap-1 p-1 text-center">
      <template v-if="mode === 'tap'">
        <Button
          type="button"
          variant="secondary"
          :size="tile ? 'icon-xs' : 'icon-lg'"
          data-testid="workspace-media-load"
          :aria-label="`${kind === 'video' ? 'Play' : 'Load'} ${label} (${sizeLabel})`"
          class="rounded-full bg-background/80 shadow-sm hover:bg-background"
          @click.prevent.stop="load"
        >
          <Play v-if="kind === 'video'" />
          <Download v-else />
        </Button>
        <span
          class="rounded-full bg-background/80 px-1.5 text-[10px] font-medium text-foreground tabular-nums"
        >
          {{ sizeLabel }}
        </span>
      </template>
      <span
        v-else
        class="rounded-md bg-background/80 px-1.5 py-0.5 text-[10px] leading-tight text-muted-foreground"
      >
        {{ TOO_LARGE }}
      </span>
    </div>
  </div>
  <ImageLightbox
    v-if="lightboxOpen && link"
    :src="link.contentUrl"
    :alt="label"
    :path="path"
    :workspace-id="workspaceId"
    :download-url="link.downloadUrl"
    @close="lightboxOpen = false"
  />
</template>
