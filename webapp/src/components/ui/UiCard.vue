<script setup lang="ts">
/**
 * UiCard — solid surface card (AP3: glass reserved for overlays only).
 */
import { cn } from '@/lib/utils'

const props = defineProps<{
  class?: string
  /** Surface elevation */
  variant?: 'default' | 'strong' | 'subtle'
}>()

const variantClass =
  props.variant === 'strong' ? 'bg-surface-elevated border border-border' :
  props.variant === 'subtle' ? 'bg-surface/60 border border-border' :
  'bg-surface border border-border'
</script>

<template>
  <div
    :class="
      cn(
        variantClass,
        'rounded-[var(--radius-lg)]',
        $props.class,
      )
    "
  >
    <slot />
  </div>
</template>
