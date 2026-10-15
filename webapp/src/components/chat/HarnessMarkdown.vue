<script setup lang="ts">
import {
  computed,
  defineComponent,
  h,
  inject,
  nextTick,
  onMounted,
  onUnmounted,
  ref,
  watch,
} from 'vue'
import type { PropType, VNodeChild } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import ImageLightbox from './ImageLightbox.vue'
import HarnessCodeBlock from './HarnessCodeBlock.vue'
import {
  classifyWorkspaceFile,
  resolveWorkspaceLinkPath,
  resolveWorkspaceMediaPath,
} from '@/lib/workspaceFileRefs'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { useFileExplorerStore } from '@/stores/fileExplorer'
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
    /** Suppress fenced-code copy controls while an assistant turn streams. */
    streaming?: boolean
  }>(),
  {
    compact: false,
    onPrimary: false,
    mentions: false,
    streaming: false,
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

type HtmlSegment = {
  kind: 'html'
  html: string
  nodes?: Node[]
  media: MediaSegment[]
}
type ImageSegment = { kind: 'image'; path: string; label: string }
type VideoSegment = { kind: 'video'; path: string; label: string }
type MediaSegment = ImageSegment | VideoSegment
type MarkdownSegment = HtmlSegment

/** Parse and sanitize the complete document so reference links remain in scope. */
function parseMarkdownRoot(text: string): { html: string; root: HTMLElement | null } {
  if (!text) return { html: '', root: null }
  const rawHtml = marked.parse(text) as string
  const sanitized = DOMPurify.sanitize(rawHtml, {
    ADD_DATA_URI_TAGS: ['img'],
    ALLOWED_URI_REGEXP:
      /^(?:(?:https?|ftp|mailto|tel|file|data|blob):|[^a-z]|[a-z+.-]+(?:[^a-z+.:-]|$))/i,
  })
  if (typeof DOMParser === 'undefined') return { html: sanitized, root: null }

  const parser = new DOMParser()
  const doc = parser.parseFromString(`<div id="md-root">${sanitized}</div>`, 'text/html')
  const root = doc.getElementById('md-root')
  if (!root) return { html: sanitized, root: null }

  for (const link of Array.from(root.querySelectorAll('a[href]'))) {
    if (workspaceId.value && resolveWorkspaceLinkPath(link.getAttribute('href') ?? '')) {
      link.removeAttribute('target')
    } else {
      link.setAttribute('target', '_blank')
    }
    link.setAttribute('rel', 'noopener noreferrer')
  }
  for (const image of Array.from(root.querySelectorAll('img[src]'))) {
    if (image.closest('a, button') || image.getAttribute('aria-hidden') === 'true') continue
    image.setAttribute('role', 'button')
    image.setAttribute('tabindex', '0')
    image.setAttribute('aria-haspopup', 'dialog')
    image.setAttribute('aria-label', `Open image preview ${image.getAttribute('alt') || 'Image'}`)
    image.classList.add(
      'cursor-zoom-in',
      'focus-visible:outline-none',
      'focus-visible:ring-2',
      'focus-visible:ring-ring',
    )
  }
  return { html: root.innerHTML, root }
}

/** Render harness markdown as sanitized HTML. */
function renderMarkdown(text: string): string {
  return parseMarkdownRoot(text).html
}

const WorkspaceMediaNode = defineComponent({
  name: 'HarnessMarkdownWorkspaceMedia',
  props: {
    kind: { type: String as PropType<MediaSegment['kind']>, required: true },
    path: { type: String, required: true },
    label: { type: String, default: '' },
    linked: { type: Boolean, default: false },
  },
  emits: { open: (segment: ImageSegment) => Boolean(segment) },
  setup(mediaProps, { emit }) {
    const store = useWorkspaceImageStore()
    const workspaceId = computed(() => workspaceIdRef.value)
    const url = computed(() => {
      const segment = { kind: mediaProps.kind, path: mediaProps.path, label: mediaProps.label }
      return segment.kind === 'image'
        ? store.getImageUrl(segment.path)
        : store.getVideoUrl(segment.path)
    })
    const loading = computed(() => {
      if (!workspaceId.value) return false
      return mediaProps.kind === 'image'
        ? store.isFetchingImage(mediaProps.path)
        : store.isFetchingVideo(mediaProps.path)
    })

    return () => {
      const label = mediaProps.label || mediaProps.path
      if (mediaProps.kind === 'image' && url.value && mediaProps.linked) {
        return h('img', {
          src: url.value,
          alt: label,
          class: 'my-2 max-h-96 max-w-full object-contain',
        })
      }
      if (mediaProps.kind === 'image' && url.value) {
        return h(
          Button,
          {
            type: 'button',
            variant: 'ghost',
            'aria-haspopup': 'dialog',
            'aria-label': `Open image preview ${label}`,
            class:
              'my-2 block h-auto max-w-full cursor-zoom-in overflow-hidden rounded-md border border-border p-0 hover:bg-transparent',
            onClick: () =>
              emit('open', { kind: 'image', path: mediaProps.path, label: mediaProps.label }),
          },
          () => [
            h('img', {
              src: url.value,
              alt: label,
              class: 'm-0 max-h-96 max-w-full object-contain',
            }),
          ],
        )
      }
      if (mediaProps.kind === 'video' && url.value) {
        return h('video', {
          src: url.value,
          controls: true,
          class: 'my-2 max-h-96 max-w-full rounded-md border border-border',
        })
      }
      if (loading.value) {
        return h(Skeleton, {
          class: 'my-2 h-24 w-full max-w-md',
          'data-testid': 'harness-markdown-media-loading',
        })
      }
      return h(
        'span',
        {
          class: 'text-muted-foreground',
          'data-testid': 'harness-markdown-media-fallback',
        },
        label,
      )
    }
  },
})

const HtmlSegmentRenderer = defineComponent({
  name: 'HarnessMarkdownHtml',
  props: {
    nodes: { type: Array as PropType<Node[]>, required: true },
    streaming: { type: Boolean, default: false },
  },
  emits: { open: (segment: ImageSegment) => Boolean(segment) },
  setup(segmentProps, { emit }) {
    return () =>
      h(
        'div',
        { 'data-md-html': '' },
        segmentProps.nodes.map((node) =>
          renderMarkdownNode(node, segmentProps.streaming, (segment) => emit('open', segment)),
        ),
      )
  },
})

function languageFromClass(codeClass: string): string {
  const languageClass = codeClass.split(/\s+/).find((name) => name.startsWith('language-'))
  return languageClass?.slice('language-'.length) ?? ''
}

function renderMarkdownNode(
  node: Node,
  isStreaming: boolean,
  onOpen: (segment: ImageSegment) => void,
): VNodeChild {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? ''
  if (node.nodeType !== Node.ELEMENT_NODE) return null

  const element = node as Element
  const tag = element.localName
  if (tag === 'pre') {
    const code = element.querySelector('code')
    if (code) {
      const preClass = element.getAttribute('class') ?? ''
      const codeClass = code.getAttribute('class') ?? ''
      return h(HarnessCodeBlock, {
        code: code.textContent ?? '',
        language: languageFromClass(codeClass),
        preClass,
        codeClass,
        disabled: isStreaming,
      })
    }
  }

  if (tag === 'img' && !element.closest('button')) {
    const path = resolveWorkspaceMediaPath(element.getAttribute('src') ?? '')
    if (path) {
      const kind = classifyWorkspaceFile(path)
      if (kind === 'image' || kind === 'video') {
        const segment: MediaSegment = {
          kind,
          path,
          label: (element.getAttribute('alt') ?? '').trim(),
        }
        return h(WorkspaceMediaNode, {
          kind: segment.kind,
          path: segment.path,
          label: segment.label,
          linked: Boolean(element.closest('a')),
          onOpen,
        })
      }
    }
  }

  const attrs: Record<string, string> = {}
  for (const attribute of Array.from(element.attributes)) {
    if (!attribute.name.toLowerCase().startsWith('on')) {
      attrs[attribute.name] = attribute.value
    }
  }
  const children = Array.from(element.childNodes).map((child) =>
    renderMarkdownNode(child, isStreaming, onOpen),
  )
  return h(tag, attrs, children)
}

function buildSegments(text: string): MarkdownSegment[] {
  const parsed = parseMarkdownRoot(text)
  const media: MediaSegment[] = []
  const nodes = parsed.root ? Array.from(parsed.root.childNodes) : undefined
  const visit = (node: Node): void => {
    if (node.nodeType !== Node.ELEMENT_NODE) return
    const element = node as Element
    if (element.localName === 'img' && !element.closest('button')) {
      const path = resolveWorkspaceMediaPath(element.getAttribute('src') ?? '')
      const kind = path ? classifyWorkspaceFile(path) : null
      if (path && (kind === 'image' || kind === 'video')) {
        media.push({ kind, path, label: (element.getAttribute('alt') ?? '').trim() })
      }
    }
    for (const child of Array.from(node.childNodes)) visit(child)
  }
  for (const node of nodes ?? []) visit(node)
  return [{ kind: 'html', html: parsed.html, nodes, media }]
}

const streaming = computed(() => props.streaming === true)
const renderedText = ref(props.text)
const segments = computed(() => buildSegments(renderedText.value))
const markdownRevision = ref(0)
const STREAM_RENDER_INTERVAL_MS = 50
let renderTimer: ReturnType<typeof setTimeout> | undefined

function clearRenderTimer(): void {
  if (renderTimer === undefined) return
  clearTimeout(renderTimer)
  renderTimer = undefined
}

function flushRenderedText(): void {
  clearRenderTimer()
  renderedText.value = props.text
}

watch(
  () => [props.text, streaming.value] as const,
  ([text, isStreaming], [previousText, wasStreaming]) => {
    if (!isStreaming) {
      // Complete/non-streaming markdown stays immediate, and the latest
      // stream delta is flushed as soon as the turn ends.
      if (text !== renderedText.value || wasStreaming) flushRenderedText()
    } else if (text !== previousText && text !== renderedText.value && renderTimer === undefined) {
      // Fixed-window coalescing bounds full-document markdown parsing while
      // streaming without attempting unsafe incremental markdown parsing.
      renderTimer = setTimeout(() => {
        renderTimer = undefined
        renderedText.value = props.text
      }, STREAM_RENDER_INTERVAL_MS)
    }
  },
  { flush: 'sync' },
)

watch(
  () => [renderedText.value, props.mentions] as const,
  ([, mentions], [, previousMentions]) => {
    if (mentions || previousMentions) markdownRevision.value += 1
  },
  { flush: 'sync' },
)

const pinnedMediaPaths = new Set<string>()
let pinnedMediaGeneration = imageStore.mediaGeneration
let pinnedWorkspaceId = workspaceId.value
const lightbox = ref<{ src: string; alt: string } | null>(null)
let lightboxMediaPath: string | null = null
let lightboxMediaGeneration = imageStore.mediaGeneration

function syncVisibleMedia(): void {
  const generation = imageStore.mediaGeneration
  const id = workspaceId.value
  const media = segments.value.flatMap((segment) => segment.media)
  const nextPaths = new Set(media.map((item) => item.path))
  const workspaceChanged = id !== pinnedWorkspaceId

  if (generation !== pinnedMediaGeneration) {
    // reset() cleared the store-side refcounts; discard old local ownership
    // without releasing it into the new generation.
    pinnedMediaPaths.clear()
    pinnedMediaGeneration = generation
    if (lightboxMediaPath && lightboxMediaGeneration !== generation) {
      lightbox.value = null
      lightboxMediaPath = null
      lightboxMediaGeneration = generation
    }
  } else if (workspaceChanged) {
    if (pinnedMediaGeneration === generation) imageStore.releaseMedia(pinnedMediaPaths)
    pinnedMediaPaths.clear()
  } else {
    const releasedPaths: string[] = []
    for (const path of pinnedMediaPaths) {
      if (!nextPaths.has(path)) {
        releasedPaths.push(path)
        pinnedMediaPaths.delete(path)
      }
    }
    if (releasedPaths.length > 0) imageStore.releaseMedia(releasedPaths)
  }
  pinnedWorkspaceId = id
  if (!id) return
  if (lightboxMediaPath && lightboxMediaGeneration !== generation) {
    lightboxMediaPath = null
    lightboxMediaGeneration = generation
  }

  // Pin all current media before requesting it, so even an oversized fresh
  // response is protected from immediate cache trimming.
  for (const path of nextPaths) {
    if (!pinnedMediaPaths.has(path)) {
      imageStore.pinMedia(path, true)
      pinnedMediaPaths.add(path)
    }
  }
  for (const item of media) {
    if (item.kind === 'image') imageStore.fetchImage(id, item.path)
    else imageStore.fetchVideo(id, item.path)
  }
}

watch(
  () => [renderedText.value, workspaceId.value, imageStore.mediaGeneration] as const,
  syncVisibleMedia,
  {
    immediate: true,
    flush: 'post',
  },
)

function mediaUrl(segment: MediaSegment): string | null {
  return segment.kind === 'image'
    ? imageStore.getImageUrl(segment.path)
    : imageStore.getVideoUrl(segment.path)
}

function openImage(segment: ImageSegment): void {
  const src = mediaUrl(segment)
  if (!src) return
  if (lightboxMediaPath !== segment.path) {
    if (lightboxMediaPath) imageStore.pinMedia(lightboxMediaPath, false)
    imageStore.pinMedia(segment.path, true)
    lightboxMediaPath = segment.path
    lightboxMediaGeneration = imageStore.mediaGeneration
  }
  lightbox.value = { src, alt: segment.label || segment.path }
}

function closeLightbox(): void {
  lightbox.value = null
  if (lightboxMediaPath && lightboxMediaGeneration === imageStore.mediaGeneration) {
    imageStore.pinMedia(lightboxMediaPath, false)
  }
  lightboxMediaPath = null
}

/** Route local file links to the existing viewer; leave browser links alone. */
function onHtmlClick(event: MouseEvent): void {
  onHtmlImageActivate(event)
  if (event.defaultPrevented || !workspaceId.value || event.button !== 0) return
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
  const target = event.target
  if (!(target instanceof Element)) return
  const link = target.closest('a[href]')
  if (!link?.closest('[data-md-html]') || !rootEl.value?.contains(link)) return
  const path = resolveWorkspaceLinkPath(link.getAttribute('href') ?? '')
  if (!path) return
  event.preventDefault()
  void useFileExplorerStore().openPath(workspaceId.value, path, { revealInExplorer: false })
}

/** Activate only sanitized HTML images, never decorative mention icons or links. */
function onHtmlImageActivate(event: MouseEvent | KeyboardEvent): void {
  if (event instanceof KeyboardEvent && event.key !== 'Enter' && event.key !== ' ') return
  const image = event.target
  if (!(image instanceof HTMLImageElement) || image.getAttribute('role') !== 'button') return
  if (!image.closest('[data-md-html]') || image.closest('a, button, [data-mention-badge]')) return
  event.preventDefault()
  image.focus()
  lightbox.value = { src: image.currentSrc || image.src, alt: image.alt || 'Image' }
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
watch(
  () => props.mentions,
  (mentions) => {
    if (!mentions) imageTokens.value = []
  },
)

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
  span.className = mentionBadgeClass + badgeTone(token.kind)
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
    badge.className = mentionBadgeClass + badgeTone(kind as 'file' | 'agent')
  }
}

function enhanceMentionBadges(): void {
  imageTokens.value = []
  if (!props.mentions) return
  const root = rootEl.value
  if (!root || typeof document === 'undefined') return
  let tokens: ComposerToken[]
  try {
    tokens = parseComposerSegments(renderedText.value).filter(
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
        if (node.nodeValue?.includes('@') && !isSkippedMentionAncestor(node as Text)) {
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
  const renderedPaths = Array.from(root.querySelectorAll('[data-mention-badge="file"]')).map(
    (badge) => badge.getAttribute('data-path'),
  )
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
  () => [renderedText.value, props.mentions, segments.value] as const,
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

onUnmounted(() => {
  clearRenderTimer()
  if (pinnedMediaGeneration === imageStore.mediaGeneration) {
    imageStore.releaseMedia(pinnedMediaPaths)
  }
  pinnedMediaPaths.clear()
  if (lightboxMediaPath && lightboxMediaGeneration === imageStore.mediaGeneration) {
    imageStore.pinMedia(lightboxMediaPath, false)
  }
  lightboxMediaPath = null
})

defineExpose({ renderMarkdown })
export type { HarnessPart }
</script>

<template>
  <div ref="rootEl" :class="rootClass" @click="onHtmlClick" @keydown="onHtmlImageActivate">
    <template v-for="(segment, index) in segments" :key="index">
      <component
        :is="HtmlSegmentRenderer"
        v-if="segment.nodes"
        :key="`${index}-${markdownRevision}`"
        :nodes="segment.nodes"
        :streaming="streaming"
        @open="openImage"
      />
      <div v-else data-md-html v-html="segment.html" />
    </template>
  </div>
  <ImageLightbox v-if="lightbox" :src="lightbox.src" :alt="lightbox.alt" @close="closeLightbox" />
</template>
