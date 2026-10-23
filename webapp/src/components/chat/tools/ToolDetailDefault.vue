<script setup lang="ts">
import { computed, ref } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { isImageAttachment, toolAttachments } from '@/lib/harnessAttachments'
import { parseToolArguments, resolveToolName, truncatePreview } from '@/lib/toolDisplay'
import ImageLightbox from '../ImageLightbox.vue'

const props = defineProps<{
  part: HarnessPart
}>()

const toolName = computed(() => resolveToolName(props.part) || props.part.title || 'tool')
const args = computed(() => parseToolArguments(props.part))
const argsPreview = computed(() =>
  Object.keys(args.value).length ? truncatePreview(JSON.stringify(args.value, null, 2), 2000) : '',
)
const preview = computed(() => {
  const output = props.part.output || ''
  try {
    return truncatePreview(JSON.stringify(JSON.parse(output), null, 2))
  } catch {
    return truncatePreview(output)
  }
})
const images = computed(() => toolAttachments(props.part).filter(isImageAttachment))
const lightbox = ref<{ src: string; alt: string } | null>(null)
</script>

<template>
  <div data-testid="tool-detail-default" class="min-w-0 space-y-1">
    <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
      {{ toolName }}
    </code>
    <div v-if="argsPreview" class="mt-1 space-y-1">
      <p class="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Input</p>
      <pre
        data-testid="tool-detail-default-input"
        class="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border/60 bg-muted/40 px-2.5 py-2 font-mono text-[11px] text-muted-foreground"
        >{{ argsPreview }}</pre
      >
    </div>
    <div v-if="preview" class="space-y-1">
      <p class="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Output</p>
      <pre
        data-testid="tool-detail-default-output"
        class="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border/60 bg-muted/40 px-2.5 py-2 font-mono text-[11px]"
        :class="part.state === 'error' ? 'text-destructive' : 'text-muted-foreground'"
        >{{ preview }}</pre
      >
    </div>
    <p v-else-if="part.state === 'running'" class="mt-1 text-[11px] text-muted-foreground">
      Running…
    </p>
    <div v-if="images.length" class="flex flex-wrap gap-2">
      <button
        v-for="(attachment, index) in images"
        :key="`${attachment.mime}-${index}`"
        type="button"
        class="overflow-hidden rounded-md border border-border bg-muted/30 transition hover:opacity-90"
        :aria-label="`Open image preview ${index + 1}`"
        @click="lightbox = { src: attachment.url, alt: `Image attachment ${index + 1}` }"
      >
        <img
          :src="attachment.url"
          :alt="`Image attachment ${index + 1}`"
          data-testid="tool-detail-default-image"
          class="max-h-32 max-w-full cursor-zoom-in object-contain"
        />
      </button>
    </div>
    <ImageLightbox
      v-if="lightbox"
      :src="lightbox.src"
      :alt="lightbox.alt"
      @close="lightbox = null"
    />
  </div>
</template>
