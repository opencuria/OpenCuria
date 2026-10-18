<script setup lang="ts">
/**
 * HarnessSteerRow — a user message the agent received mid-run ("steered").
 *
 * Sits inline in the assistant turn at the point the agent picked it up:
 * the same right-aligned bubble as a regular user message, plus a tiny muted
 * caption so it reads as sent while the agent was working. Read-only: no
 * edit or fork actions.
 */
import { CornerDownRight } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import HarnessUserBubble from './HarnessUserBubble.vue'

defineProps<{ part: HarnessPart }>()
</script>

<template>
  <div class="flex justify-end py-1" data-testid="harness-steer-row">
    <div class="min-w-0 max-w-[85%]">
      <HarnessUserBubble :text="part.output" />
      <p
        class="mt-1 flex items-center justify-end gap-1 text-[11px] text-muted-foreground"
        data-testid="harness-steer-caption"
      >
        <CornerDownRight :size="11" aria-hidden="true" />
        Sent while working
      </p>
    </div>
  </div>
</template>
