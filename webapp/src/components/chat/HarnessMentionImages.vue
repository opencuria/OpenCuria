<script setup lang="ts">
/**
 * HarnessMentionImages — compact image thumbnails for `@file:` mention tokens.
 *
 * Published prop API for composer reuse:
 * - `tokens`: file tokens (`{ kind:'file', path, raw?, name?, start, end }`,
 *   as returned by `parseComposerSegments` / `imageMentionTokens` in
 *   `@opencuria/chat-core/composerTokens`). Only safe `/workspace/...` image
 *   paths render; escapes and non-images are ignored.
 * - `workspaceId`: workspace the images load from. Empty/absent shows
 *   fallback tiles only, never a stuck spinner.
 * - `removable`: show an X per thumbnail that emits `remove` (composer
 *   preview). History always leaves this false (no X in history).
 * - `onPrimary`: adjust fallback/border tones for dark primary bubbles.
 * - `token.label`: optional display name (e.g. a tool image's source file);
 *   defaults to the workspace-relative path.
 *
 * Emits:
 * - `remove(start, end)`: token offsets for `removeComposerToken(text,start,end)`.
 *
 * Each thumbnail is a `WorkspaceMedia` tile (tap-to-load for large images,
 * lightbox on click).
 */
import { computed } from 'vue'
import { X } from '@lucide/vue'
import WorkspaceFileIcon from '@/components/files/WorkspaceFileIcon.vue'
import WorkspaceMedia from './WorkspaceMedia.vue'
import { classifyWorkspaceFile } from '@opencuria/chat-core/workspaceFileRefs'
import { workspaceRelativePath } from '@opencuria/chat-core/harnessMentions'

export interface MentionFileToken {
  kind: string
  path: string
  raw?: string
  name?: string
  label?: string
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

function displayName(path: string, label?: string): string {
  if (label) return label
  try {
    const relative = workspaceRelativePath(path)
    return relative || path.split('/').pop() || path
  } catch {
    return path.split('/').pop() || path
  }
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
      <WorkspaceMedia
        :workspace-id="workspaceId"
        :path="token.path"
        kind="image"
        :alt="displayName(token.path, token.label)"
        variant="tile"
        :on-primary="onPrimary"
      >
        <template #fallback>
          <span
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
            <span class="w-full truncate text-center">{{
              displayName(token.path, token.label)
            }}</span>
          </span>
        </template>
      </WorkspaceMedia>
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
</template>
