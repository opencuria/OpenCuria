<script setup lang="ts">
import { computed, ref } from 'vue'
import { FileText } from '@lucide/vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments, stringArg, truncatePreview } from '@/lib/toolDisplay'
import {
  attachmentSizeLabel,
  isImageAttachment,
  isPdfAttachment,
  toolAttachments,
  type ToolAttachment,
} from '@/lib/harnessAttachments'
import ImageLightbox from '../ImageLightbox.vue'

const props = defineProps<{
  part: HarnessPart
}>()

function safeBasename(value: unknown): string {
  if (typeof value !== 'string' || !value) return ''
  const trimmed = value.replace(/\/+$/, '')
  if (!trimmed) return ''
  const slash = trimmed.lastIndexOf('/')
  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}

function attachmentFileName(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) return ''
  const trimmed = value.trim()
  return safeBasename(trimmed) || trimmed
}

const metaPath = computed(() => {
  try {
    const fromMeta = props.part?.meta?.['path']
    if (typeof fromMeta === 'string' && fromMeta) return fromMeta
    return ''
  } catch {
    return ''
  }
})

const argsPath = computed(() => {
  try {
    if (!props.part) return ''
    return stringArg(parseToolArguments(props.part), 'path')
  } catch {
    return ''
  }
})

const path = computed(() => metaPath.value || argsPath.value)

const preview = computed(() => {
  try {
    const output = props.part?.output
    return truncatePreview(typeof output === 'string' ? output : String(output ?? ''))
  } catch {
    return ''
  }
})

const attachments = computed<ToolAttachment[]>(() => {
  try {
    return toolAttachments(props.part)
  } catch {
    return []
  }
})
const images = computed(() => attachments.value.filter(isImageAttachment))
const pdfs = computed(() => attachments.value.filter(isPdfAttachment))

/**
 * Filename priority: attachment.filename > basename(meta.path) >
 * basename(tool args path) > "document.pdf".
 */
function pdfFileNameFor(attachment: ToolAttachment): string {
  try {
    const fromAttachment = attachmentFileName(
      (attachment as { filename?: unknown } | null)?.filename,
    )
    if (fromAttachment) return fromAttachment
  } catch {
    // fall through to path fallbacks
  }
  const fromMeta = safeBasename(metaPath.value)
  if (fromMeta) return fromMeta
  const fromArgs = safeBasename(argsPath.value)
  if (fromArgs) return fromArgs
  return 'document.pdf'
}

function imageAlt(attachment: ToolAttachment | undefined, index: number): string {
  let fromAttachment = ''
  try {
    fromAttachment = attachment ? attachmentFileName(attachment.filename) : ''
  } catch {
    fromAttachment = ''
  }
  const base = fromAttachment || safeBasename(path.value)
  return base ? `${base} (image ${index + 1})` : `Image attachment ${index + 1}`
}

function pdfLabel(attachment: ToolAttachment): string {
  try {
    const size = attachmentSizeLabel(attachment.url)
    return size ? `PDF attachment (${size})` : 'PDF attachment'
  } catch {
    return 'PDF attachment'
  }
}

const lightbox = ref<{ src: string; alt: string } | null>(null)

function openLightbox(attachment: ToolAttachment, index: number): void {
  try {
    lightbox.value = { src: attachment.url, alt: imageAlt(attachment, index) }
  } catch {
    lightbox.value = null
  }
}

function closeLightbox(): void {
  lightbox.value = null
}
</script>

<template>
  <div data-testid="tool-detail-read" class="min-w-0 space-y-1">
    <code
      v-if="path"
      class="block truncate font-mono text-[11px] text-muted-foreground"
    >{{ path }}</code>
    <div v-if="images.length" class="flex flex-wrap gap-2">
      <button
        v-for="(attachment, index) in images"
        :key="`${attachment.mime}-${index}`"
        type="button"
        class="overflow-hidden rounded-md border border-border bg-muted/30 transition hover:opacity-90"
        :aria-label="`Open image preview ${index + 1}`"
        @click="openLightbox(attachment, index)"
      >
        <img
          :src="attachment.url"
          :alt="imageAlt(attachment, index)"
          data-testid="tool-detail-read-image"
          class="max-h-32 max-w-full cursor-zoom-in object-contain"
        />
      </button>
    </div>
    <div v-if="pdfs.length" class="space-y-1">
      <a
        v-for="(attachment, index) in pdfs"
        :key="`${attachment.mime}-${index}`"
        :href="attachment.url"
        :download="pdfFileNameFor(attachment)"
        target="_blank"
        rel="noopener noreferrer"
        data-testid="tool-detail-read-pdf"
        class="flex items-center gap-1.5 font-mono text-[11px] text-primary hover:underline"
      >
        <FileText :size="12" class="shrink-0" />
        <span class="truncate">{{ pdfLabel(attachment) }} · {{ pdfFileNameFor(attachment) }}</span>
      </a>
    </div>
    <pre
      v-if="preview"
      class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 px-2 py-1.5 font-mono text-[11px] text-muted-foreground"
    >{{ preview }}</pre>
    <p v-else-if="part?.state === 'running'" class="text-[11px] text-muted-foreground">Reading…</p>
    <ImageLightbox
      v-if="lightbox"
      :src="lightbox.src"
      :alt="lightbox.alt"
      @close="closeLightbox"
    />
  </div>
</template>
