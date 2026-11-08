<script setup lang="ts">
import { computed, inject, ref } from 'vue'

import type { HarnessPart } from '@opencuria/chat-core/harness'
import { workspaceAttachmentTokens } from '@/lib/harnessAttachments'
import { isImageAttachment, toolAttachments } from '@opencuria/chat-core/harnessAttachments'
import { parseToolArguments, stringArg, truncatePreview } from '@opencuria/chat-core/toolDisplay'
import HarnessMentionImages from '../HarnessMentionImages.vue'
import ImageLightbox from '../ImageLightbox.vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { resolveWorkspaceMediaPath } from '@opencuria/chat-core/workspaceFileRefs'

const props = defineProps<{
  part: HarnessPart
}>()
const workspaceId = inject(harnessWorkspaceIdKey, ref(''))

const args = computed(() => parseToolArguments(props.part))
const prompt = computed(() => stringArg(args.value, 'prompt'))
const transparentBackground = computed(() => args.value.transparent_background === true)
const result = computed<Record<string, unknown> | null>(() => {
  try {
    const parsed: unknown = JSON.parse(props.part.output || '')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null
  } catch {
    return null
  }
})
const savedPath = computed(() => {
  const value = result.value?.savedPath ?? result.value?.saved_path
  return typeof value === 'string' ? value : ''
})
const pointingPath = computed(() =>
  savedPath.value.startsWith('/workspace/') &&
  resolveWorkspaceMediaPath(savedPath.value) === savedPath.value
    ? savedPath.value
    : undefined,
)
const status = computed(() => {
  const value = result.value?.status
  return typeof value === 'string' ? value : ''
})
const preview = computed(() => {
  if (
    result.value &&
    !result.value.failure &&
    Object.keys(result.value).every((key) =>
      ['savedPath', 'saved_path', 'status', 'failure'].includes(key),
    )
  )
    return ''
  return truncatePreview(props.part.output || '')
})
const attachments = computed(() => toolAttachments(props.part).filter(isImageAttachment))
const images = computed(() => attachments.value.filter((attachment) => attachment.url))
const workspaceImages = computed(() =>
  workspaceAttachmentTokens(
    attachments.value,
    (_attachment, index) => `Generated image ${index + 1}`,
  ),
)
const lightbox = ref<{ src: string; alt: string } | null>(null)
</script>

<template>
  <div data-testid="tool-detail-image-generation" class="min-w-0 space-y-2">
    <div v-if="prompt" class="space-y-1">
      <p class="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Prompt</p>
      <p class="whitespace-pre-wrap break-words text-[11px] text-foreground/90">{{ prompt }}</p>
    </div>
    <p v-if="transparentBackground" class="text-[11px] text-muted-foreground">
      Transparent background
    </p>
    <div v-if="savedPath || status" class="space-y-1 text-[11px]">
      <p v-if="savedPath" class="break-all">
        <span class="text-muted-foreground">Saved to </span>
        <code class="font-mono text-foreground/90">{{ savedPath }}</code>
      </p>
      <p v-if="status" class="text-muted-foreground">{{ status }}</p>
    </div>
    <div v-if="images.length" class="flex flex-wrap gap-2">
      <button
        v-for="(attachment, index) in images"
        :key="`${attachment.mime}-${index}`"
        type="button"
        class="overflow-hidden rounded-md border border-border bg-muted/30 transition hover:opacity-90"
        :aria-label="`Open generated image preview ${index + 1}`"
        @click="lightbox = { src: attachment.url!, alt: `Generated image ${index + 1}` }"
      >
        <img
          :src="attachment.url"
          :alt="`Generated image ${index + 1}`"
          :data-voice-image-path="pointingPath"
          :data-voice-workspace="pointingPath ? workspaceId : undefined"
          data-testid="tool-detail-image-generation-image"
          class="max-h-32 max-w-full cursor-zoom-in object-contain"
        />
      </button>
    </div>
    <HarnessMentionImages
      v-if="workspaceImages.length"
      :tokens="workspaceImages"
      :workspace-id="workspaceId"
    />
    <pre
      v-if="preview"
      data-testid="tool-detail-image-generation-output"
      class="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border/60 bg-muted/40 px-2.5 py-2 font-mono text-[11px]"
      :class="part.state === 'error' ? 'text-destructive' : 'text-muted-foreground'"
      >{{ preview }}</pre
    >
    <p v-else-if="part.state === 'running'" class="text-[11px] text-muted-foreground">
      Generating image…
    </p>
    <ImageLightbox
      v-if="lightbox"
      :src="lightbox.src"
      :alt="lightbox.alt"
      :path="pointingPath"
      :workspace-id="pointingPath ? workspaceId : undefined"
      @close="lightbox = null"
    />
  </div>
</template>
