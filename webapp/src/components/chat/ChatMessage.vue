<script setup lang="ts">
import { computed, watch, ref, onMounted, onUnmounted, nextTick } from 'vue'
import type { Session } from '@/types'
import { SessionStatus } from '@/types'
import { User, BookText, Copy, Volume2, Square, MailOpen, Mail } from 'lucide-vue-next'
import { UiSpinner, ImageLightbox, UiButton } from '@/components/ui'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { useNotificationStore } from '@/stores/notifications'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { isSessionActive, isSessionDone, isSessionFailed } from '@/lib/sessionState'

const props = defineProps<{
  session: Session
  workspaceId?: string
}>()

const emit = defineEmits<{
  toggleReadState: [sessionId: string]
}>()

const imageStore = useWorkspaceImageStore()
const fileExplorerStore = useFileExplorerStore()
const notifications = useNotificationStore()

// Root element ref for IntersectionObserver
const rootEl = ref<HTMLElement | null>(null)
// Becomes true once this message has entered the viewport at least once.
const hasBeenVisible = ref(false)

function fetchImages(): void {
  if (!props.workspaceId) return
  for (const path of extractWorkspaceImagePaths(props.session.prompt ?? '')) {
    imageStore.fetchImage(props.workspaceId, path)
  }
  for (const path of extractWorkspaceImagePaths(props.session.output ?? '')) {
    imageStore.fetchImage(props.workspaceId, path)
  }
}

let intersectionObserver: IntersectionObserver | null = null
let codeCopyResetTimer: ReturnType<typeof setTimeout> | null = null

onMounted(() => {
  intersectionObserver = new IntersectionObserver(
    (entries) => {
      if (entries[0]?.isIntersecting) {
        hasBeenVisible.value = true
        fetchImages()
      }
    },
    { rootMargin: '300px 0px', threshold: 0 },
  )
  if (rootEl.value) intersectionObserver.observe(rootEl.value)
})

onUnmounted(() => {
  intersectionObserver?.disconnect()
  if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
    window.speechSynthesis.cancel()
  }
  if (codeCopyResetTimer) {
    clearTimeout(codeCopyResetTimer)
    codeCopyResetTimer = null
  }
})

// Lightbox state
const lightboxSrc = ref<string | null>(null)
const lightboxAlt = ref<string>('')
const pendingVideoAutoplay = ref<Set<string>>(new Set())

function closeLightbox() {
  lightboxSrc.value = null
  lightboxAlt.value = ''
}

function getFilename(path: string): string {
  return path.split('/').pop() || path
}

function escapeHtmlAttr(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

function encodeBase64(text: string): string {
  const bytes = new TextEncoder().encode(text)
  let binary = ''
  for (const byte of bytes) {
    binary += String.fromCharCode(byte)
  }
  return btoa(binary)
}

function decodeBase64(encoded: string): string {
  const binary = atob(encoded)
  const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0))
  return new TextDecoder().decode(bytes)
}

async function copyToClipboard(text: string): Promise<void> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(textarea)
  if (!copied) {
    throw new Error('Clipboard API is not available in this browser context.')
  }
}

function requestVideoLoad(path: string): void {
  if (!props.workspaceId || !imageStore.isVideoPath(path)) return
  pendingVideoAutoplay.value.add(path)
  imageStore.fetchVideo(props.workspaceId, path)
}

async function tryAutoplayPendingVideos(): Promise<void> {
  if (!pendingVideoAutoplay.value.size || !rootEl.value) return

  const toRemove: string[] = []
  const videoEls = Array.from(
    rootEl.value.querySelectorAll('video[data-ws-video-path]'),
  ) as HTMLVideoElement[]

  for (const path of pendingVideoAutoplay.value) {
    const video = videoEls.find((el) => el.dataset.wsVideoPath === path)
    if (!video) continue
    try {
      await video.play()
    } catch {
      // Playback may still require an additional explicit user interaction in some browsers.
    }
    toRemove.push(path)
  }

  for (const path of toRemove) {
    pendingVideoAutoplay.value.delete(path)
  }
}

const isStreaming = computed(() => isSessionActive(props.session.status))

const isFailed = computed(() => isSessionFailed(props.session.status))
const copiedPrompt = ref(false)
const copiedOutput = ref(false)
const speakingSource = ref<'prompt' | 'output' | null>(null)
const isUnread = computed(() => !props.session.read_at && !isStreaming.value)

async function copyMessage(content: string, source: 'prompt' | 'output'): Promise<void> {
  const text = content.trim()
  if (!text) return

  try {
    await copyToClipboard(text)

    if (source === 'prompt') {
      copiedPrompt.value = true
      setTimeout(() => (copiedPrompt.value = false), 1200)
      notifications.success('Prompt copied', 'Message copied to clipboard.')
    } else {
      copiedOutput.value = true
      setTimeout(() => (copiedOutput.value = false), 1200)
      notifications.success('Response copied', 'Message copied to clipboard.')
    }
  } catch (error) {
    const detail = error instanceof Error ? error.message : 'Could not copy to clipboard.'
    notifications.error('Copy failed', detail)
  }
}

function stopSpeech(): void {
  if (typeof window === 'undefined' || !('speechSynthesis' in window)) return
  window.speechSynthesis.cancel()
  speakingSource.value = null
}

function toggleReadState(): void {
  const nextState = isUnread.value ? 'read' : 'unread'
  emit('toggleReadState', props.session.id)
  notifications.success(
    nextState === 'read' ? 'Marked as read' : 'Marked as unread',
    nextState === 'read'
      ? 'The reply was marked as read again.'
      : 'The reply will stay unread until you reopen the chat.',
  )
}

function toggleSpeak(content: string, source: 'prompt' | 'output'): void {
  const text = content.trim()
  if (!text) return

  if (speakingSource.value === source) {
    stopSpeech()
    return
  }

  if (typeof window === 'undefined' || !('speechSynthesis' in window)) {
    notifications.error('Speech unavailable', 'Your browser does not support text-to-speech.')
    return
  }

  stopSpeech()
  const utterance = new SpeechSynthesisUtterance(text)
  utterance.lang = 'de-DE'
  utterance.rate = 1

  utterance.onend = () => {
    speakingSource.value = null
  }
  utterance.onerror = () => {
    speakingSource.value = null
    notifications.error('Speech failed', 'Could not read the message aloud.')
  }

  speakingSource.value = source
  window.speechSynthesis.speak(utterance)
}

function enhanceMarkdownHtml(html: string): string {
  if (typeof DOMParser === 'undefined') return html

  const parser = new DOMParser()
  const doc = parser.parseFromString(`<div id="md-root">${html}</div>`, 'text/html')
  const root = doc.getElementById('md-root')
  if (!root) return html

  for (const link of Array.from(root.querySelectorAll('a[href]'))) {
    link.setAttribute('target', '_blank')
    link.setAttribute('rel', 'noopener noreferrer')
  }

  for (const pre of Array.from(root.querySelectorAll('pre'))) {
    const code = pre.querySelector('code')
    if (!code) continue
    const codeText = code.textContent ?? ''
    if (!codeText.trim()) continue

    const wrapper = doc.createElement('div')
    wrapper.className = 'ws-code-wrapper'
    pre.parentNode?.insertBefore(wrapper, pre)
    wrapper.appendChild(pre)

    const copyButton = doc.createElement('button')
    copyButton.type = 'button'
    copyButton.className = 'ws-code-copy-btn'
    copyButton.setAttribute('data-code-copy', encodeBase64(codeText))
    copyButton.setAttribute('aria-label', 'Copy code snippet')
    copyButton.innerHTML =
      '<span class="ws-code-copy-default" aria-hidden="true">⧉</span>' +
      '<span class="ws-code-copy-done" aria-hidden="true">✓</span>'
    wrapper.appendChild(copyButton)
  }

  return root.innerHTML
}

function onOutputClick(e: MouseEvent) {
  const target = e.target as HTMLElement

  const codeCopyBtn = target.closest('.ws-code-copy-btn') as HTMLElement | null
  if (codeCopyBtn) {
    const encoded = codeCopyBtn.getAttribute('data-code-copy')
    if (encoded) {
      try {
        const decoded = decodeBase64(encoded)
        copyToClipboard(decoded)
          .then(() => {
            codeCopyBtn.classList.add('is-copied')
            if (codeCopyResetTimer) clearTimeout(codeCopyResetTimer)
            codeCopyResetTimer = setTimeout(() => {
              codeCopyBtn.classList.remove('is-copied')
            }, 1400)
            notifications.success('Code copied', 'Snippet copied to clipboard.')
          })
          .catch((error) => {
            const detail = error instanceof Error ? error.message : 'Could not copy code snippet.'
            notifications.error('Copy failed', detail)
          })
      } catch {
        notifications.error('Copy failed', 'Could not decode code snippet.')
      }
    }
    e.preventDefault()
    return
  }

  const videoLoadBtn = target.closest('.ws-video-load') as HTMLElement | null
  if (videoLoadBtn) {
    const path = videoLoadBtn.getAttribute('data-ws-video-path')
    if (path) {
      requestVideoLoad(path)
      e.preventDefault()
    }
    return
  }

  if (target.tagName === 'IMG') {
    const img = target as HTMLImageElement
    if (img.src) {
      lightboxSrc.value = img.src
      lightboxAlt.value = img.alt ?? ''
      e.preventDefault()
    }
    return
  }

  const link = target.closest('a') as HTMLAnchorElement | null
  if (!link || !props.workspaceId) return

  const href = link.getAttribute('href') ?? ''
  const url = link.href ? new URL(link.href, window.location.origin) : null
  const workspacePath = href.startsWith('/workspace')
    ? href.replace(/[?#].*$/, '')
    : url?.pathname.startsWith('/workspace')
      ? url.pathname
      : null

  if (workspacePath) {
    e.preventDefault()
    const revealInExplorer = window.matchMedia('(min-width: 1024px)').matches
    fileExplorerStore.openPath(props.workspaceId, workspacePath, {
      revealInExplorer,
    }).catch(() => {
      notifications.error(
        'Could not open file',
        'The linked workspace file could not be opened in the explorer.',
      )
    })
  }
}

// Regex to match ![alt](/workspace/path/to/file.ext)
const WS_MEDIA_PATTERN = /!\[([^\]]*)\]\((\/workspace\/[^)]+)\)/g

function extractWorkspaceImagePaths(text: string): string[] {
  const paths: string[] = []
  let m
  while ((m = WS_MEDIA_PATTERN.exec(text)) !== null) {
    const path = m[2]
    if (path && imageStore.isImagePath(path)) {
      paths.push(path)
    }
  }
  WS_MEDIA_PATTERN.lastIndex = 0
  return paths
}

function renderMarkdown(text: string): string {
  if (!text) return ''

  const processed = text.replace(WS_MEDIA_PATTERN, (_match, altText, path) => {
    const alt = String(altText || '')

    if (imageStore.isImagePath(path)) {
      const url = imageStore.getImageUrl(path)
      if (url) return `![${alt}](${url})`
      if (imageStore.isFetchingImage(path)) {
        return `<span class="ws-img-loading" title="Loading ${escapeHtmlAttr(alt || 'image')}..." aria-label="Loading image"></span>`
      }
      return `![${alt}](${path})`
    }

    if (imageStore.isVideoPath(path)) {
      const escapedPath = escapeHtmlAttr(path)
      const escapedLabel = escapeHtmlAttr(alt || getFilename(path))

      if (imageStore.isFetchingVideo(path)) {
        return `<button type="button" class="ws-video-load ws-video-loading" data-ws-video-path="${escapedPath}" aria-label="Loading video ${escapedLabel}"><span class="ws-video-spinner" aria-hidden="true"></span><span>Loading video...</span></button>`
      }

      const videoUrl = imageStore.getVideoUrl(path)
      if (!videoUrl) {
        return `<button type="button" class="ws-video-load" data-ws-video-path="${escapedPath}" aria-label="Load and play video ${escapedLabel}">▶ Play video: ${escapedLabel}</button>`
      }

      const sourceType = imageStore.getVideoMimeType(path)
      const typeAttr = sourceType ? ` type="${escapeHtmlAttr(sourceType)}"` : ''
      return `<video class="ws-video-player" controls preload="metadata" data-ws-video-path="${escapedPath}"><source src="${escapeHtmlAttr(videoUrl)}"${typeAttr}></video>`
    }

    return `![${alt}](${path})`
  })

  WS_MEDIA_PATTERN.lastIndex = 0
  const rawHtml = marked.parse(processed) as string

  const sanitized = DOMPurify.sanitize(rawHtml, {
    ADD_TAGS: ['video', 'source'],
    ADD_ATTR: ['controls', 'preload', 'data-ws-video-path', 'type'],
    ADD_DATA_URI_TAGS: ['img', 'video', 'source'],
    ALLOWED_URI_REGEXP: /^(?:(?:https?|ftp|mailto|tel|file|data|blob):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i,
  })

  const normalized = sanitized
    .replace(/<table(\b[^>]*)>/gi, '<div class="table-scroll-wrapper"><table$1>')
    .replace(/<\/table>/gi, '</table></div>')

  return enhanceMarkdownHtml(normalized)
}

const renderedPrompt = computed(() => renderMarkdown(props.session.prompt))
const renderedOutput = computed(() => renderMarkdown(props.session.output))

watch(
  [() => props.session.prompt, () => props.session.output],
  () => {
    if (hasBeenVisible.value) fetchImages()
  },
)

watch(
  [renderedPrompt, renderedOutput],
  async () => {
    await nextTick()
    await tryAutoplayPendingVideos()
  },
)

const hasSkills = computed(() => props.session.skills && props.session.skills.length > 0)
</script>

<template>
  <div ref="rootEl" class="flex flex-col gap-3">
    <!-- User prompt -->
    <div class="flex items-start gap-3 justify-end">
      <div class="flex flex-col items-end gap-1.5">
        <div class="max-w-[80%] sm:max-w-[70%]">
          <div
            class="prose-output overflow-x-auto rounded-[var(--radius-md)] rounded-br-sm bg-primary text-primary-fg px-4 py-3 text-sm break-words prose prose-invert prose-sm prose-p:my-0 prose-headings:my-1 prose-pre:bg-primary/20 prose-pre:text-primary-fg"
            v-html="renderedPrompt"
            @click="onOutputClick"
          ></div>
        </div>

        <div v-if="session.prompt?.trim()" class="flex items-center justify-end gap-0.5 w-full max-w-[80%] sm:max-w-[70%]">
          <UiButton
            variant="ghost"
            size="icon-sm"
            class="chat-action-btn text-primary-fg/75 hover:text-primary-fg"
            :title="copiedPrompt ? 'Copied!' : 'Copy message'"
            @click="copyMessage(session.prompt, 'prompt')"
          >
            <Copy :size="13" :class="copiedPrompt ? 'text-success' : ''" />
          </UiButton>
          <UiButton
            variant="ghost"
            size="icon-sm"
            class="chat-action-btn text-primary-fg/75 hover:text-primary-fg"
            :title="speakingSource === 'prompt' ? 'Stop reading' : 'Read aloud'"
            @click="toggleSpeak(session.prompt, 'prompt')"
          >
            <component :is="speakingSource === 'prompt' ? Square : Volume2" :size="13" />
          </UiButton>
        </div>

        <!-- Skill badges: shown under the user prompt bubble -->
        <div v-if="hasSkills" class="flex flex-wrap gap-1 justify-end max-w-[80%] sm:max-w-[70%]">
          <span
            v-for="sk in session.skills"
            :key="sk.id"
            class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-primary/10 text-primary text-[10px] font-medium border border-primary/20"
            :title="sk.body"
          >
            <BookText :size="9" />
            {{ sk.name }}
          </span>
        </div>
      </div>

      <div
        class="flex items-center justify-center w-8 h-8 rounded-full bg-primary/10 text-primary shrink-0"
      >
        <User :size="14" />
      </div>
    </div>

    <!-- Agent output -->
    <div class="flex items-start gap-3">
      <div
        :class="[
          'flex-1 min-w-0 py-2 text-sm',
          isFailed
            ? 'bg-error-muted border border-error/20 text-error rounded-[var(--radius-md)] rounded-bl-sm px-4 py-3'
            : 'text-fg',
        ]"
      >
        <!-- Output content -->
        <div
          v-if="session.output"
          class="prose-output prose prose-sm max-w-none break-words prose-p:leading-relaxed prose-pre:p-2 prose-pre:bg-muted prose-pre:text-muted-fg prose-pre:rounded-md dark:prose-invert prose-headings:text-fg prose-p:text-fg prose-strong:text-fg prose-ul:text-fg prose-ol:text-fg prose-li:text-fg prose-a:text-primary prose-code:text-fg prose-blockquote:text-muted-fg prose-blockquote:border-l-primary prose-img:rounded-md prose-img:max-w-full prose-img:cursor-pointer prose-img:hover:opacity-90 prose-img:transition-opacity"
          v-html="renderedOutput"
          @click="onOutputClick"
        ></div>

        <!-- Streaming indicator -->
        <div v-if="isStreaming && !session.output" class="flex items-center gap-2 text-muted-fg">
          <UiSpinner :size="14" />
          <span class="text-xs">{{ session.status_detail || 'Agent is thinking…' }}</span>
        </div>

        <!-- Streaming cursor at end of output -->
        <span
          v-if="isStreaming && session.output"
          class="inline-block w-2 h-4 bg-primary/60 animate-pulse ml-0.5 align-middle"
        />

        <!-- Empty completed state -->
        <span
          v-if="!session.output && session.status === SessionStatus.COMPLETED"
          class="text-muted-fg italic text-xs"
        >
          No output returned.
        </span>

        <div v-if="session.output?.trim()" class="flex items-center gap-0.5 mt-1.5 -ml-1">
          <UiButton
            variant="ghost"
            size="icon-sm"
            class="chat-action-btn"
            :title="copiedOutput ? 'Copied!' : 'Copy message'"
            @click="copyMessage(session.output, 'output')"
          >
            <Copy :size="13" :class="copiedOutput ? 'text-success' : ''" />
          </UiButton>
          <UiButton
            variant="ghost"
            size="icon-sm"
            class="chat-action-btn"
            :title="speakingSource === 'output' ? 'Stop reading' : 'Read aloud'"
            @click="toggleSpeak(session.output, 'output')"
          >
            <component :is="speakingSource === 'output' ? Square : Volume2" :size="13" />
          </UiButton>
          <UiButton
            v-if="isSessionDone(session.status)"
            variant="ghost"
            size="icon-sm"
            class="chat-action-btn chat-action-btn-unread"
            :class="{ 'is-unread': isUnread }"
            :title="isUnread ? 'Mark as read again' : 'Mark as unread'"
            @click="toggleReadState"
          >
            <component :is="isUnread ? Mail : MailOpen" :size="13" />
          </UiButton>
          <span
            v-if="session.agent_model"
            class="text-[11px] text-muted-fg/90 ml-1.5 pl-1.5 border-l border-border/60 truncate max-w-[14rem]"
            :title="session.agent_model"
          >
            {{ session.agent_model }}
          </span>
        </div>
      </div>
    </div>

    <!-- Image lightbox -->
    <ImageLightbox
      v-if="lightboxSrc"
      :src="lightboxSrc"
      :alt="lightboxAlt"
      @close="closeLightbox"
    />
  </div>
</template>

<style scoped>
.chat-action-btn {
  width: 1.75rem;
  height: 1.75rem;
  border: 0;
  box-shadow: none;
  background: transparent;
  color: var(--color-muted-fg);
  padding: 0;
  min-width: 1.75rem;
}

.chat-action-btn:hover {
  background: transparent;
  color: var(--color-fg);
}

.chat-action-btn-unread.is-unread {
  color: color-mix(in oklab, var(--color-fg) 78%, var(--color-primary) 22%);
}

:deep(.ws-img-loading) {
  display: inline-block;
  width: 1.25rem;
  height: 1.25rem;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: ws-spin 0.7s linear infinite;
  vertical-align: middle;
  opacity: 0.45;
  margin: 0.1em 0.25em;
}

:deep(.ws-video-load) {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8125rem;
  color: var(--color-primary);
  background: color-mix(in oklab, var(--color-primary) 8%, transparent);
  border: 1px solid color-mix(in oklab, var(--color-primary) 25%, transparent);
  border-radius: 0.5rem;
  padding: 0.4rem 0.6rem;
  cursor: pointer;
}

:deep(.ws-video-loading) {
  cursor: progress;
  opacity: 0.9;
}

:deep(.ws-video-spinner) {
  display: inline-block;
  width: 0.9rem;
  height: 0.9rem;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: ws-spin 0.7s linear infinite;
}

:deep(.ws-video-player) {
  display: block;
  max-width: 100%;
  width: min(100%, 42rem);
  border-radius: 0.5rem;
  margin: 0.35rem 0;
}

:deep(.ws-code-wrapper) {
  position: relative;
}

:deep(.ws-code-copy-btn) {
  position: absolute;
  top: 0.38rem;
  right: 0.4rem;
  border: 0;
  background: transparent;
  color: var(--color-muted-fg);
  font-size: 0.9rem;
  line-height: 1;
  padding: 0.2rem 0.25rem;
  border-radius: 0.375rem;
  cursor: pointer;
  opacity: 0;
  transition: opacity 180ms ease, color 180ms ease;
}

:deep(.ws-code-wrapper:hover .ws-code-copy-btn),
:deep(.ws-code-copy-btn:focus-visible),
:deep(.ws-code-copy-btn.is-copied) {
  opacity: 1;
}

:deep(.ws-code-copy-btn:hover) {
  color: var(--color-fg);
}

:deep(.ws-code-copy-done) {
  display: none;
}

:deep(.ws-code-copy-btn.is-copied .ws-code-copy-default) {
  display: none;
}

:deep(.ws-code-copy-btn.is-copied .ws-code-copy-done) {
  display: inline;
  color: var(--color-success);
}

@keyframes ws-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
