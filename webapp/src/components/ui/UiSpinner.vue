<script setup lang="ts">
import { computed } from 'vue'
import { Loader2 } from 'lucide-vue-next'
import { cn } from '@/lib/utils'

const props = withDefaults(
  defineProps<{
    size?: number
    class?: string
  }>(),
  { size: 20 },
)

const classes = computed(() => cn('animate-spin text-muted-fg', props.class))
</script>

<template>
  <Loader2 :size="size" :class="classes" />
</template>
