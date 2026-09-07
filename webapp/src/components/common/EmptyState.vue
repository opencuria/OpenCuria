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
      class="mb-3 flex size-10 items-center justify-center rounded-md bg-muted text-muted-foreground"
    >
      <component :is="icon" :size="18" aria-hidden="true" />
    </div>
    <p class="text-sm font-medium text-foreground">{{ title }}</p>
    <p v-if="description" class="mt-1 max-w-sm text-sm text-muted-foreground">
      {{ description }}
    </p>
    <div v-if="$slots.action" class="mt-4">
      <slot name="action" />
    </div>
  </div>
</template>
