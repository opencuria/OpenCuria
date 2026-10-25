<script setup lang="ts">
import type { HTMLAttributes } from 'vue'
import { computed } from 'vue'
import { cn } from '@/lib/utils'
import { USAGE_LEVEL_CLASSES, type UsageLevel } from '@/lib/workspaceDisk'

/** Slim horizontal usage bar; the fill colour follows the usage level. */
const props = withDefaults(
  defineProps<{
    percent: number
    level?: UsageLevel
    label: string
    class?: HTMLAttributes['class']
  }>(),
  { level: 'ok' },
)

const fill = computed(() => Math.min(100, Math.max(0, props.percent)))
</script>

<template>
  <div
    :class="cn('h-1.5 overflow-hidden rounded-full bg-muted', props.class)"
    role="progressbar"
    :aria-label="label"
    :aria-valuenow="Math.round(fill)"
    aria-valuemin="0"
    aria-valuemax="100"
  >
    <div
      class="h-full rounded-full transition-[width] duration-500"
      :class="USAGE_LEVEL_CLASSES[level].fill"
      :style="{ width: `${fill}%` }"
    />
  </div>
</template>
