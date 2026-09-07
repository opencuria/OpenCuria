<script setup lang="ts">
import type { HTMLAttributes } from 'vue'
import { computed } from 'vue'
import { cn } from '@/lib/utils'

const props = defineProps<{
  title?: string
  description?: string
  class?: HTMLAttributes['class']
}>()

const hasHeader = computed(() => Boolean(props.title || props.description))
</script>

<template>
  <section :class="cn('space-y-4', props.class)">
    <div v-if="hasHeader || $slots.actions" class="flex items-start justify-between gap-3">
      <div class="min-w-0 space-y-1">
        <h3 v-if="title" class="text-sm font-semibold text-foreground">{{ title }}</h3>
        <p v-if="description" class="text-sm text-muted-foreground">{{ description }}</p>
      </div>
      <div v-if="$slots.actions" class="shrink-0">
        <slot name="actions" />
      </div>
    </div>
    <slot />
  </section>
</template>
