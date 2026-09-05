<script setup lang="ts">
import { Loader2 } from '@lucide/vue'
import { cn } from '@/lib/utils'

withDefaults(
  defineProps<{
    class?: string
    size?: number
  }>(),
  {
    class: '',
    size: 20,
  },
)
</script>

<template>
  <Loader2
    :size="size"
    :class="cn('animate-spin text-muted-foreground', $props.class)"
    aria-hidden="true"
  />
</template>
