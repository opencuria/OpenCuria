<script setup lang="ts">
import type { Component, HTMLAttributes } from 'vue'
import { cn } from '@/lib/utils'

const props = defineProps<{
  icon?: Component
  title: string
  description?: string
  class?: HTMLAttributes['class']
}>()
</script>

<template>
  <div
    :class="
      cn('flex flex-col items-center justify-center px-6 py-12 text-center', props.class)
    "
  >
    <div
      v-if="icon"
      class="mb-4 flex size-11 items-center justify-center rounded-xl border border-border bg-card text-foreground/70 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_4px_12px_-4px_rgb(0_0_0/0.08)]"
    >
      <component :is="icon" :size="20" :stroke-width="1.75" aria-hidden="true" />
    </div>
    <p class="text-sm font-semibold text-foreground">{{ title }}</p>
    <p v-if="description" class="mt-1 max-w-sm text-sm text-pretty text-muted-foreground">
      {{ description }}
    </p>
    <div v-if="$slots.action" class="mt-5 flex flex-wrap items-center justify-center gap-2">
      <slot name="action" />
    </div>
  </div>
</template>
