<script setup lang="ts">
import { computed, inject, nextTick, onMounted, ref, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { Skeleton } from '@/components/ui/skeleton'
import { classifyWorkspaceFile, resolveWorkspaceMediaPath } from '@/lib/workspaceFileRefs'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import {
  parseComposerSegments,
  type ComposerFileToken,
  type ComposerToken,
} from '@/lib/composerTokens'
import { workspaceFileIconUrl } from '@/lib/fileIconAssets'
import type { HarnessPart } from '@/types/harness'

const emit = defineEmits<{ 'mention-images': [tokens: ComposerFileToken[]] }>()

const props = withDefaults(
  defineProps<{
    text: string
    compact?: boolean
    /** White text for dark primary backgrounds (user prompt bubble). */
    onPrimary?: boolean
    /**
     * Render `@file:` / `@agent:` mention tokens as inline badges plus
     * image tokens reported to the message view. Only the user history bubble sets
     * this; assistant responses keep plain markdown (`false`).
     */
    mentions?: boolean
  }>(),
  {
    compact: false,
    onPrimary: false,
    mentions: false,
  },
)

const rootClass = computed(() => {
  const classes = [
    'prose-output prose prose-sm max-w-3xl break-words',
    'prose-pre:p-2 prose-pre:rounded-md',
  ]
  if (props.compact) {
    classes.push('text-[13px] [&_p]:my-0 prose-p:leading-snug')
  } else {
    classes.push('prose-p:leading-relaxed')
  }
  if (props.onPrimary) {
    classes.push(
      'prose-on-primary',
      'prose-pre:bg-primary-foreground/15 prose-pre:text-primary-foreground',
      'prose-headings:text-primary-foreground',
      'prose-p:text-primary-foreground',
      'prose-strong:text-primary-foreground',
      'prose-ul:text-primary-foreground',
      'prose-ol:text-primary-foreground',
      'prose-li:text-primary-foreground',
      'prose-a:text-primary-foreground prose-a:underline',
      'prose-code:text-primary-foreground',
      'prose-blockquote:text-primary-foreground/80 prose-blockquote:border-l-primary-foreground',
    )
    return classes
  }
  classes.push(
    'prose-pre:bg-muted prose-pre:text-muted-foreground',
    'dark:prose-invert',
    'prose-headings:text-foreground',
    'prose-p:text-foreground',
    'prose-strong:text-foreground',
    'prose-ul:text-foreground',
    'prose-ol:text-foreground',
    'prose-li:text-foreground',
    'prose-a:text-primary',
    'prose-code:text-foreground',
    'prose-blockquote:text-muted-foreground prose-blockquote:border-l-primary',
  )
  return classes
})

const workspaceIdRef = inject(harnessWorkspaceIdKey, ref(''))
const workspaceId = computed(() => workspaceIdRef.value)
const imageStore = useWorkspaceImageStore()

const MARKDOWN_IMAGE_RE = /!\[([^\]]*)\]\(([^)]+)\)/g

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

  MARKDOWN_IMAGE_RE.lastIndex = 0
  while ((match = MARKDOWN_IMAGE_RE.exec(text)) !== null) {
    const path = resolveWorkspaceMediaPath(match[2] ?? '')
    if (!path) continue
    const fileKind = classifyWorkspaceFile(path)
    if (fileKind !== 'image' && fileKind !== 'video') continue

    const before = text.slice(lastIndex, match.index)
    if (before) {
      segments.push({ kind: 'html', html: renderMarkdown(before) })
    }
    segments.push({
      kind: fileKind,
      path,
      label: (match[1] ?? '').trim(),
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

// ---------------------------------------------------------------------------
// Mention rendering (sent-user history only, `mentions === true`).
//
// Tokens come from the shared `src/lib/composerTokens.ts`
// (`parseComposerSegments` / `imageMentionTokens`) and badge icons from the
// shared `src/lib/fileIconAssets.ts` (`workspaceFileIconUrl`), so history
// badges match composer chips. Badges are applied as a safe DOM text-node
// enhancement after marked+DOMPurify render the HTML (code blocks and links
// are skipped); `data-md-html` containers are re-rendered from `segments`
// whenever the text changes, so badges can never go stale.
// ---------------------------------------------------------------------------

// Derive previews from badges actually rendered in prose, not raw prompt text:
// a mention inside inline/fenced code or a link must not fetch an image.
const imageTokens = ref<ComposerFileToken[]>([])
watch(imageTokens, (tokens) => emit('mention-images', tokens))

const rootEl = ref<HTMLElement | null>(null)

function badgeTone(kind: 'file' | 'agent'): string {
  if (props.onPrimary) {
    return kind === 'file'
      ? ' bg-primary-foreground/15 text-primary-foreground border-primary-foreground/30'
      : ' bg-primary-foreground/15 text-primary-foreground border-primary-foreground/30 font-semibold'
  }
  return kind === 'file'
    ? ' bg-muted text-foreground border-border'
    : ' bg-primary/10 text-primary border-primary/20'
}

const mentionBadgeClass =
  'mention-badge inline-flex h-5 max-w-[min(100%,12rem)] items-center gap-1 rounded-md border px-1.5 align-middle text-[11px] leading-none font-medium whitespace-nowrap'

function createMentionBadge(token: ComposerToken): HTMLElement {
  const span = document.createElement('span')
  span.setAttribute('data-mention-badge', token.kind)
  span.setAttribute(
    'data-testid',
    token.kind === 'file' ? 'mention-badge-file' : 'mention-badge-agent',
  )
  if (token.kind === 'file') {
    span.setAttribute('data-path', token.path)
    span.setAttribute('title', token.path)
  } else {
    span.setAttribute('data-agent', token.name)
    span.setAttribute('title', `@agent:${token.name}`)
  }
  span.className =
    mentionBadgeClass + badgeTone(token.kind)
  if (token.kind === 'file') {
    try {
      const url = workspaceFileIconUrl(token.path)
      if (url) {
        const img = document.createElement('img')
        img.setAttribute('src', url)
        img.setAttribute('alt', '')
        img.setAttribute('aria-hidden', 'true')
        img.setAttribute('draggable', 'false')
        img.setAttribute('data-testid', 'mention-badge-icon')
        img.className = 'shrink-0 object-contain'
        img.style.width = '12px'
        img.style.height = '12px'
        span.appendChild(img)
      }
    } catch {
      // Icon is decorative; badge text still renders.
    }
    // Badge label is the filename only; the full path stays in `title`.
    const label = document.createElement('span')
    label.textContent = token.name
    label.className = 'min-w-0 truncate'
    span.appendChild(label)
  } else {
    span.textContent = `@agent:${token.name}`
  }
  return span
}

function isSkippedMentionAncestor(node: Node): boolean {
  let el = node.parentElement
  while (el) {
    const tag = el.tagName
    if (
      tag === 'CODE' ||
      tag === 'PRE' ||
      tag === 'A' ||
      tag === 'SCRIPT' ||
      tag === 'STYLE' ||
      tag === 'TEXTAREA'
    ) {
      return true
    }
    if (el.hasAttribute?.('data-mention-badge')) return true
    if (el.hasAttribute?.('data-md-html')) break
    el = el.parentElement
  }
  return false
}

function replaceTokensInTextNode(textNode: Text, tokens: ComposerToken[]): void {
  const text = textNode.nodeValue ?? ''
  if (!text.includes('@')) return
  const matches: Array<{ index: number; token: ComposerToken }> = []
  for (const token of tokens) {
    const raw = token.raw
    if (!raw || !text.includes(raw)) continue
    let from = 0
    while (true) {
      const idx = text.indexOf(raw, from)
      if (idx < 0) break
      const before = idx > 0 ? (text[idx - 1] ?? '') : ''
      const after = idx + raw.length < text.length ? (text[idx + raw.length] ?? '') : ''
      const beforeOk = !before || /\s/.test(before) || before === '(' || before === '['
      const afterOk = !after || /\s/.test(after) || /[.,;:!?)\]]/.test(after)
      if (beforeOk && afterOk) matches.push({ index: idx, token })
      from = idx + raw.length
    }
  }
  if (matches.length === 0) return
  matches.sort((a, b) => a.index - b.index || b.token.raw.length - a.token.raw.length)
  const filtered: typeof matches = []
  let lastEnd = -1
  for (const m of matches) {
    if (m.index < lastEnd) continue
    filtered.push(m)
    lastEnd = m.index + m.token.raw.length
  }
  if (filtered.length === 0) return
  const frag = document.createDocumentFragment()
  let cursor = 0
  for (const m of filtered) {
    if (m.index > cursor) {
      frag.appendChild(document.createTextNode(text.slice(cursor, m.index)))
    }
    try {
      frag.appendChild(createMentionBadge(m.token))
    } catch {
      frag.appendChild(document.createTextNode(m.token.raw))
    }
    cursor = m.index + m.token.raw.length
  }
  if (cursor < text.length) {
    frag.appendChild(document.createTextNode(text.slice(cursor)))
  }
  textNode.parentNode?.replaceChild(frag, textNode)
}

function refreshMentionBadgeTones(): void {
  const root = rootEl.value
  if (!root || typeof document === 'undefined') return
  for (const badge of Array.from(root.querySelectorAll('[data-mention-badge]'))) {
    const kind = badge.getAttribute('data-mention-badge') === 'agent' ? 'agent' : 'file'
    badge.className =
      mentionBadgeClass + badgeTone(kind as 'file' | 'agent')
  }
}

function enhanceMentionBadges(): void {
  imageTokens.value = []
  if (!props.mentions) return
  const root = rootEl.value
  if (!root || typeof document === 'undefined') return
  let tokens: ComposerToken[]
  try {
    tokens = parseComposerSegments(props.text).filter(
      (segment): segment is ComposerToken => segment.kind !== 'text',
    )
  } catch {
    return
  }
  if (tokens.length === 0) return
  const containers = root.querySelectorAll('[data-md-html]')
  if (containers.length === 0) return
  for (const container of Array.from(containers)) {
    // Collect text nodes manually (works in browsers and jsdom); the
    // containers re-render from `segments` on text changes, so a single
    // enhancement pass per render is enough and badges never go stale.
    const textNodes: Text[] = []
    const visit = (node: Node): void => {
      if (node.nodeType === 3) {
        if (
          node.nodeValue?.includes('@') &&
          !isSkippedMentionAncestor(node as Text)
        ) {
          textNodes.push(node as Text)
        }
        return
      }
      for (const child of Array.from(node.childNodes)) visit(child)
    }
    visit(container)
    for (const textNode of textNodes) {
      replaceTokensInTextNode(textNode, tokens)
    }
  }
  const renderedPaths = Array.from(root.querySelectorAll('[data-mention-badge="file"]'))
    .map((badge) => badge.getAttribute('data-path'))
  imageTokens.value = tokens.filter(
    (token): token is ComposerFileToken =>
      token.kind === 'file' &&
      classifyWorkspaceFile(token.path) === 'image' &&
      renderedPaths.includes(token.path),
  )
}

function scheduleMentionEnhance(): void {
  if (!props.mentions || typeof document === 'undefined') return
  void nextTick(() => {
    try {
      enhanceMentionBadges()
    } catch {
      // Badge enhancement is cosmetic; never break markdown rendering.
    }
  })
}

// The `segments` computed re-renders the `data-md-html` containers on text
// changes, so one post-render pass is enough (no retry timers).
watch(
  () => [props.text, props.mentions, segments.value] as const,
  () => {
    scheduleMentionEnhance()
  },
)

watch(
  () => props.onPrimary,
  () => {
    void nextTick(() => {
      refreshMentionBadgeTones()
    })
  },
)

onMounted(() => {
  scheduleMentionEnhance()
})

defineExpose({ renderMarkdown })
export type { HarnessPart }
</script>

<template>
  <div ref="rootEl" :class="rootClass">
    <template v-for="(segment, index) in segments" :key="index">
      <div v-if="segment.kind === 'html'" data-md-html v-html="segment.html" />
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
