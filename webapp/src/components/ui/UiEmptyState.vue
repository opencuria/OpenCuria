<script setup lang="ts">
/**
 * Empty state placeholder for lists with no data.
 */
import { cn } from '@/lib/utils'

defineProps<{
  title: string
  description?: string
  class?: string
}>()
</script>

<template>
  <div
    :class="
      cn(
        'flex flex-col items-center justify-center py-12 px-6 text-center',
        $props.class,
      )
    "
  >
    <div class="mb-4 text-muted-fg">
      <slot name="icon" />
    </div>
    <h3 class="text-lg font-medium text-fg mb-1">{{ title }}</h3>
    <p v-if="description" class="text-sm text-muted-fg max-w-sm">
      {{ description }}
    </p>
    <div class="mt-4">
      <slot name="action" />
    </div>
  </div>
</template>
