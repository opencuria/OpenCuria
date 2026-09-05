<script setup lang="ts">
import { computed, inject, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { Skeleton } from '@/components/ui/skeleton'
import { classifyWorkspaceFile } from '@/lib/workspaceFileRefs'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import type { HarnessPart } from '@/types/harness'

const props = defineProps<{
  text: string
  compact?: boolean
}>()

const workspaceIdRef = inject(harnessWorkspaceIdKey, ref(''))
const workspaceId = computed(() => workspaceIdRef.value)
const imageStore = useWorkspaceImageStore()

const WORKSPACE_MEDIA_RE =
  /!\[([^\]]*)\]\((\/workspace\/[^)\s]+(?: [^)]+)?)\)/g

type HtmlSegment = { kind: 'html'; html: string }
type ImageSegment = { kind: 'image'; path: string; label: string }
type VideoSegment = { kind: 'video'; path: string; label: string }
type MediaSegment = ImageSegment | VideoSegment
type MarkdownSegment = HtmlSegment | MediaSegment

/** Render harness markdown (marked + DOMPurify, links open in a new tab). */
function renderMarkdown(text: string): string {
  if (!text) return ''
  const rawHtml = marked.parse(text) as string
  const sanitized = DOMPurify.sanitize(rawHtml, {
    ADD_DATA_URI_TAGS: ['img'],
    ALLOWED_URI_REGEXP:
      /^(?:(?:https?|ftp|mailto|tel|file|data|blob):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i,
  })
  if (typeof DOMParser === 'undefined') return sanitized
  const parser = new DOMParser()
  const doc = parser.parseFromString(`<div id="md-root">${sanitized}</div>`, 'text/html')
  const root = doc.getElementById('md-root')
  if (!root) return sanitized
  for (const link of Array.from(root.querySelectorAll('a[href]'))) {
    link.setAttribute('target', '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
  }
  return root.innerHTML
}

function buildSegments(text: string): MarkdownSegment[] {
  if (!text) return [{ kind: 'html', html: '' }]

  const segments: MarkdownSegment[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null

  WORKSPACE_MEDIA_RE.lastIndex = 0
  while ((match = WORKSPACE_MEDIA_RE.exec(text)) !== null) {
    const label = (match[1] ?? '').trim()
    const rawPath = (match[2] ?? '').trim().replace(/^<|>$/g, '')
    const fileKind = classifyWorkspaceFile(rawPath)
    if (fileKind !== 'image' && fileKind !== 'video') continue

    const before = text.slice(lastIndex, match.index)
    if (before) {
      segments.push({ kind: 'html', html: renderMarkdown(before) })
    }
    segments.push({
      kind: fileKind,
      path: rawPath,
      label,
    })
    lastIndex = match.index + match[0].length
  }

  const tail = text.slice(lastIndex)
  if (tail || segments.length === 0) {
    segments.push({ kind: 'html', html: renderMarkdown(tail) })
  }

  return segments
}

const segments = computed(() => buildSegments(props.text))

watch(
  () => [props.text, workspaceId.value] as const,
  () => {
    const id = workspaceId.value
    if (!id) return
    for (const segment of segments.value) {
      if (segment.kind === 'image') {
        imageStore.fetchImage(id, segment.path)
      } else if (segment.kind === 'video') {
        imageStore.fetchVideo(id, segment.path)
      }
    }
  },
  { immediate: true },
)

function mediaUrl(segment: MediaSegment): string | null {
  return segment.kind === 'image'
    ? imageStore.getImageUrl(segment.path)
    : imageStore.getVideoUrl(segment.path)
}

function isMediaLoading(segment: MediaSegment): boolean {
  if (!workspaceId.value) return false
  return segment.kind === 'image'
    ? imageStore.isFetchingImage(segment.path)
    : imageStore.isFetchingVideo(segment.path)
}

function showMediaFallback(segment: MediaSegment): boolean {
  if (!workspaceId.value) return true
  if (mediaUrl(segment)) return false
  return !isMediaLoading(segment)
}

defineExpose({ renderMarkdown })
export type { HarnessPart }
</script>

<template>
  <div
    class="prose-output prose prose-sm max-w-3xl break-words prose-p:leading-relaxed prose-pre:p-2 prose-pre:bg-muted prose-pre:text-muted-foreground prose-pre:rounded-md dark:prose-invert prose-headings:text-foreground prose-p:text-foreground prose-strong:text-foreground prose-ul:text-foreground prose-ol:text-foreground prose-li:text-foreground prose-a:text-primary prose-code:text-foreground prose-blockquote:text-muted-foreground prose-blockquote:border-l-primary"
    :class="compact ? 'text-[13px]' : ''"
  >
    <template v-for="(segment, index) in segments" :key="index">
      <div v-if="segment.kind === 'html'" v-html="segment.html" />
      <template v-else>
        <img
          v-if="segment.kind === 'image' && mediaUrl(segment)"
          :src="mediaUrl(segment)!"
          :alt="segment.label || segment.path"
          class="my-2 max-h-96 max-w-full rounded-md border border-border object-contain"
        />
        <video
          v-else-if="segment.kind === 'video' && mediaUrl(segment)"
          :src="mediaUrl(segment)!"
          controls
          class="my-2 max-h-96 max-w-full rounded-md border border-border"
        />
        <Skeleton
          v-else-if="isMediaLoading(segment)"
          class="my-2 h-24 w-full max-w-md"
          data-testid="harness-markdown-media-loading"
        />
        <span
          v-else-if="showMediaFallback(segment)"
          class="text-muted-foreground"
          data-testid="harness-markdown-media-fallback"
        >
          {{ segment.label || segment.path }}
        </span>
      </template>
    </template>
  </div>
</template>
