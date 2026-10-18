<script setup lang="ts">
/**
 * Idle-gap activity indicator: "Thinking" with a left-to-right shimmer
 * while the assistant turn is busy but no tool/subtask is running.
 * Same icon + label layout as a tool row so the timeline stays aligned.
 */
import { Lightbulb } from '@lucide/vue'
</script>

<template>
  <p data-testid="harness-thinking" class="flex items-center gap-2 py-[3px] text-xs">
    <Lightbulb :size="13" class="shrink-0 text-muted-foreground opacity-60" aria-hidden="true" />
    <span class="harness-shimmer">Thinking</span>
  </p>
</template>
