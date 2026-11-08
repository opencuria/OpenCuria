<script setup lang="ts">
/** Status pill of an image definition or runner build; spins while the state changes. */
import { Loader2 } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import type { ImageStatus, ImageStatusTone } from '@opencuria/chat-core/imageDefinitionLifecycle'

defineProps<{ status: ImageStatus }>()

const TONE_CLASSES: Record<ImageStatusTone, string> = {
  success: 'bg-success/10 text-success',
  progress: 'bg-primary/10 text-primary',
  danger: 'bg-destructive/10 text-destructive',
  muted: 'bg-muted text-muted-foreground',
}
</script>

<template>
  <Badge
    as="span"
    variant="secondary"
    :class="TONE_CLASSES[status.tone]"
    :data-tone="status.tone"
    data-testid="image-status"
  >
    <Loader2 v-if="status.tone === 'progress'" class="animate-spin" aria-hidden="true" />
    <span v-else class="size-1.5 rounded-full bg-current" aria-hidden="true" />
    {{ status.label }}
  </Badge>
</template>
