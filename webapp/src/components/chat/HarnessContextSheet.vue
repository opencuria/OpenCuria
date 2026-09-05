<script setup lang="ts">
import { computed } from 'vue'
import { X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { formatTokenCount } from '@/lib/sessionContextUsage'
import type { ContextSheetState } from '@/lib/composerSheets'

const props = defineProps<{
  context: ContextSheetState
  /** Non-interactive when rendered as a peek edge below a higher-priority sheet. */
  peek?: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const hasLimit = computed(() => props.context.limit > 0)
const fillPercent = computed(() =>
  hasLimit.value ? Math.min(100, Math.max(0, props.context.percent)) : 0,
)
const usageLabel = computed(() =>
  hasLimit.value ? `${props.context.percent}% Full` : 'Unknown limit',
)
const tokenSummary = computed(() => {
  if (!hasLimit.value) {
    return `~${formatTokenCount(props.context.used)} Tokens`
  }
  return `~${formatTokenCount(props.context.used)} / ${formatTokenCount(props.context.limit)} Tokens`
})
const showBreakdown = computed(
  () =>
    (props.context.promptTokens ?? 0) > 0 ||
    (props.context.completionTokens ?? 0) > 0,
)
</script>

<template>
  <div class="px-4 py-3" data-testid="composer-context-sheet">
    <div class="flex items-center justify-between gap-2">
      <h3 class="text-sm font-medium text-foreground">Context Usage</h3>
      <Button
        v-if="!peek"
        type="button"
        variant="ghost"
        size="icon"
        class="h-7 w-7 shrink-0 text-muted-foreground"
        title="Close context usage"
        data-testid="composer-context-close"
        @click="emit('close')"
      >
        <X :size="14" />
      </Button>
    </div>

    <div class="mt-3 flex items-baseline justify-between gap-3 text-sm">
      <span class="font-medium text-foreground" data-testid="composer-context-percent">
        {{ usageLabel }}
      </span>
      <span class="text-muted-foreground" data-testid="composer-context-tokens">
        {{ tokenSummary }}
      </span>
    </div>

    <div
      class="mt-3 h-2 overflow-hidden rounded-full bg-muted"
      role="progressbar"
      :aria-valuenow="fillPercent"
      aria-valuemin="0"
      aria-valuemax="100"
      data-testid="composer-context-bar"
    >
      <div
        class="h-full rounded-full bg-primary transition-all"
        :style="{ width: `${fillPercent}%` }"
      />
    </div>

    <div
      v-if="showBreakdown"
      class="mt-3 flex flex-col gap-1.5 text-xs text-muted-foreground"
      data-testid="composer-context-breakdown"
    >
      <div class="flex items-center justify-between gap-2">
        <span>Input</span>
        <span>{{ formatTokenCount(context.promptTokens ?? 0) }}</span>
      </div>
      <div class="flex items-center justify-between gap-2">
        <span>Output</span>
        <span>{{ formatTokenCount(context.completionTokens ?? 0) }}</span>
      </div>
    </div>
  </div>
</template>
