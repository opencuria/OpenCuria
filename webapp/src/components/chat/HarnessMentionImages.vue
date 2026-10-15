<script setup lang="ts">
/**
 * HarnessMentionImages — compact image thumbnails for `@file:` mention tokens.
 *
 * Published prop API for composer reuse:
 * - `tokens`: file tokens (`{ kind:'file', path, raw?, name?, start, end }`,
 *   as returned by `parseComposerSegments` / `imageMentionTokens` in the
 *   planned `src/lib/composerTokens.ts`). Only safe `/workspace/...` image
 *   paths render; escapes and non-images are ignored.
 * - `workspaceId`: workspace used for `store.fetchImage`. Empty/absent
 *   disables fetching (fallback tiles only, never a stuck spinner).
 * - `removable`: show an X per thumbnail that emits `remove` (composer
 *   preview). History always leaves this false (no X in history).
 * - `onPrimary`: adjust fallback/border tones for dark primary bubbles.
 *
 * Emits:
 * - `remove(start, end)`: token offsets for `removeComposerToken(text,start,end)`.
 *
 * Uses `store.fetchImage` / `getImageUrl` / `isFetchingImage`; click opens
 * the existing `ImageLightbox`.
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { X } from '@lucide/vue'
import { Skeleton } from '@/components/ui/skeleton'
import WorkspaceFileIcon from '@/components/files/WorkspaceFileIcon.vue'
import ImageLightbox from './ImageLightbox.vue'
import { classifyWorkspaceFile } from '@/lib/workspaceFileRefs'
import { workspaceRelativePath } from '@/lib/harnessMentions'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'

export interface MentionFileToken {
  kind: string
  path: string
  raw?: string
  name?: string
  start: number
  end: number
}

const props = withDefaults(
  defineProps<{
    tokens: MentionFileToken[]
    workspaceId: string
    removable?: boolean
    onPrimary?: boolean
    stripClass?: string
  }>(),
  {
    removable: false,
    onPrimary: false,
    stripClass: '',
  },
)

const emit = defineEmits<{
  remove: [start: number, end: number]
}>()

const imageStore = useWorkspaceImageStore()

function posixNormalize(path: string): string {
  const absolute = path.startsWith('/')
  const out: string[] = []
  for (const part of path.split('/')) {
    if (!part || part === '.') continue
    if (part === '..') {
      if (out.length > 0) out.pop()
      continue
    }
    out.push(part)
  }
  const joined = out.join('/')
  return absolute ? `/${joined}` : joined
}

/** Reject sandbox escapes and non-workspace paths (never fetch/render). */
function isSafeWorkspacePath(path: unknown): path is string {
  if (typeof path !== 'string' || !path) return false
  if (path.includes('\\') || path.includes('\0')) return false
  if (path !== '/workspace' && !path.startsWith('/workspace/')) return false
  const normalized = posixNormalize(path)
  if (normalized !== '/workspace' && !normalized.startsWith('/workspace/')) return false
  if (normalized === '/workspace') return false
  return true
}

const safeImages = computed(() =>
  (props.tokens ?? []).filter(
    (token) =>
      token && isSafeWorkspacePath(token.path) && classifyWorkspaceFile(token.path) === 'image',
  ),
)

function displayName(path: string): string {
  try {
    const relative = workspaceRelativePath(path)
    return relative || path.split('/').pop() || path
  } catch {
    return path.split('/').pop() || path
  }
}

function imageUrl(path: string): string | null {
  try {
    return imageStore.getImageUrl(path)
  } catch {
    return null
  }
}

function isLoading(path: string): boolean {
  if (!props.workspaceId) return false
  try {
    return imageStore.isFetchingImage(path)
  } catch {
    return false
  }
}

const pinnedPaths = new Set<string>()
let pinnedMediaGeneration = imageStore.mediaGeneration
let pinnedWorkspaceId = props.workspaceId
const lightbox = ref<{ src: string; alt: string } | null>(null)
let lightboxPath: string | null = null
let lightboxMediaGeneration = imageStore.mediaGeneration

watch(
  () =>
    [
      safeImages.value.map((token) => token.path),
      props.workspaceId,
      imageStore.mediaGeneration,
    ] as const,
  ([paths, workspaceId, generation]) => {
    const nextPaths = new Set(paths)
    const generationChanged = generation !== pinnedMediaGeneration
    const workspaceChanged = workspaceId !== pinnedWorkspaceId
    if (generationChanged) {
      // reset() cleared the store's pins; never release old ownership into the
      // newly reset pin table.
      pinnedPaths.clear()
      pinnedMediaGeneration = generation
    } else if (workspaceChanged) {
      imageStore.releaseMedia(pinnedPaths)
      pinnedPaths.clear()
    } else {
      const releasedPaths: string[] = []
      for (const path of pinnedPaths) {
        if (!nextPaths.has(path)) {
          releasedPaths.push(path)
          pinnedPaths.delete(path)
        }
      }
      if (releasedPaths.length > 0) imageStore.releaseMedia(releasedPaths)
    }
    pinnedWorkspaceId = workspaceId
    if (lightboxPath && lightboxMediaGeneration !== generation) {
      lightbox.value = null
      lightboxPath = null
      lightboxMediaGeneration = generation
    }
    for (const path of nextPaths) {
      if (!pinnedPaths.has(path)) {
        imageStore.pinMedia(path, true)
        pinnedPaths.add(path)
      }
    }
  },
  { immediate: true, flush: 'sync' },
)

watch(
  () =>
    [
      safeImages.value.map((token) => token.path),
      props.workspaceId,
      imageStore.mediaGeneration,
    ] as const,
  () => {
    const id = props.workspaceId
    if (!id) return
    for (const token of safeImages.value) {
      try {
        imageStore.fetchImage(id, token.path)
      } catch {
        // Never let a fetch failure break history rendering.
      }
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (pinnedMediaGeneration === imageStore.mediaGeneration) {
    imageStore.releaseMedia(pinnedPaths)
  }
  pinnedPaths.clear()
  if (lightboxPath && lightboxMediaGeneration === imageStore.mediaGeneration) {
    imageStore.pinMedia(lightboxPath, false)
  }
  lightboxPath = null
})

function openLightbox(path: string): void {
  const url = imageUrl(path)
  if (!url) return
  if (lightboxPath !== path) {
    if (lightboxPath && lightboxMediaGeneration === imageStore.mediaGeneration) {
      imageStore.pinMedia(lightboxPath, false)
    }
    imageStore.pinMedia(path, true)
    lightboxPath = path
    lightboxMediaGeneration = imageStore.mediaGeneration
  }
  lightbox.value = { src: url, alt: displayName(path) }
}

function closeLightbox(): void {
  lightbox.value = null
  if (lightboxPath && lightboxMediaGeneration === imageStore.mediaGeneration) {
    imageStore.pinMedia(lightboxPath, false)
  }
  lightboxPath = null
}

function requestRemove(token: MentionFileToken): void {
  emit('remove', token.start, token.end)
}
</script>

<template>
  <div
    v-if="safeImages.length > 0"
    data-testid="mention-images"
    class="flex min-w-0 flex-wrap gap-1.5"
    :class="stripClass"
  >
    <div
      v-for="token in safeImages"
      :key="`${token.path}-${token.start}`"
      class="relative w-20 shrink-0"
    >
      <button
        v-if="imageUrl(token.path)"
        type="button"
        data-testid="mention-image-button"
        :aria-label="`Open image preview ${displayName(token.path)}`"
        class="flex h-14 w-full items-center justify-center overflow-hidden rounded-lg border bg-muted/30 shadow-sm transition-colors hover:border-primary/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        :class="onPrimary ? 'border-primary-foreground/30' : 'border-border'"
        @click="openLightbox(token.path)"
      >
        <img
          :src="imageUrl(token.path)!"
          :alt="displayName(token.path)"
          :data-path="token.path"
          data-testid="mention-image"
          class="h-full w-full cursor-zoom-in object-contain"
          draggable="false"
        />
      </button>
      <Skeleton
        v-else-if="isLoading(token.path)"
        data-testid="mention-image-loading"
        class="h-14 w-full rounded-lg"
      />
      <span
        v-else
        data-testid="mention-image-fallback"
        :data-path="token.path"
        class="flex h-14 w-full flex-col items-center justify-center gap-1 overflow-hidden rounded-lg border px-1.5 text-[10px]"
        :class="
          onPrimary
            ? 'border-primary-foreground/30 bg-primary-foreground/10 text-primary-foreground'
            : 'border-border bg-muted/40 text-muted-foreground'
        "
        :title="token.path"
      >
        <WorkspaceFileIcon :path="token.path" :size="14" />
        <span class="w-full truncate text-center">{{ displayName(token.path) }}</span>
      </span>
      <button
        v-if="removable"
        type="button"
        data-testid="mention-image-remove"
        aria-label="Remove image"
        class="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full border shadow-sm transition hover:opacity-90"
        :class="
          onPrimary
            ? 'border-primary-foreground/30 bg-primary text-primary-foreground'
            : 'border-border bg-card text-muted-foreground hover:text-foreground'
        "
        @click.stop="requestRemove(token)"
      >
        <X :size="12" />
      </button>
    </div>
  </div>
  <ImageLightbox v-if="lightbox" :src="lightbox.src" :alt="lightbox.alt" @close="closeLightbox" />
</template>
