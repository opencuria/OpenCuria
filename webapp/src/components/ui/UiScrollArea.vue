<script setup lang="ts">
import { cn } from '@/lib/utils'

defineProps<{
  class?: string
}>()
</script>

<template>
  <div :class="cn('overflow-y-auto overflow-x-hidden', $props.class)">
    <slot />
  </div>
</template>
