<script setup lang="ts">
import { computed, ref } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { isImageAttachment, toolAttachments } from '@/lib/harnessAttachments'
import { resolveToolName, truncatePreview } from '@/lib/toolDisplay'
import ImageLightbox from '../ImageLightbox.vue'

const props = defineProps<{
  part: HarnessPart
}>()

const toolName = computed(() => resolveToolName(props.part) || props.part.title || 'tool')
const preview = computed(() => truncatePreview(props.part.output || ''))
const images = computed(() => toolAttachments(props.part).filter(isImageAttachment))
const lightbox = ref<{ src: string; alt: string } | null>(null)
</script>

<template>
  <div data-testid="tool-detail-default" class="min-w-0 space-y-1">
    <code class="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
      {{ toolName }}
    </code>
    <pre
      v-if="preview"
      class="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px]"
      :class="part.state === 'error' ? 'text-destructive' : 'text-muted-foreground'"
      >{{ preview }}</pre
    >
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
