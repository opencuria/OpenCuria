<script setup lang="ts">
import { AlertCircle, Info, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import type { NoticeSheetState } from '@/lib/composerSheets'

defineProps<{
  notice: NoticeSheetState
  /** Non-interactive when rendered as a peek edge below a higher-priority sheet. */
  peek?: boolean
}>()

const emit = defineEmits<{
  dismiss: [messageId: string]
}>()
</script>

<template>
  <div class="flex items-start gap-2 px-4 py-3" data-testid="composer-notice-sheet">
    <AlertCircle
      v-if="notice.tone === 'error'"
      :size="14"
      class="mt-0.5 shrink-0 text-destructive"
    />
    <Info v-else :size="14" class="mt-0.5 shrink-0 text-muted-foreground" />
    <p
      class="min-w-0 flex-1 text-sm"
      :class="notice.tone === 'error' ? 'text-destructive' : 'text-foreground'"
      data-testid="composer-notice-text"
    >
      {{ notice.text }}
    </p>
    <Button
      v-if="!peek"
      type="button"
      variant="ghost"
      size="icon"
      class="h-7 w-7 shrink-0 text-muted-foreground"
      title="Dismiss"
      data-testid="composer-notice-dismiss"
      @click="emit('dismiss', notice.messageId)"
    >
      <X :size="14" />
    </Button>
  </div>
</template>
