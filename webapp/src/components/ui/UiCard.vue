<script setup lang="ts">
/**
 * UiCard — Liquid Glass card with squircle geometry.
 *
 * Uses the three-layer optical stack:
 * 1. Illumination via ::after (ambient colour bleed)
 * 2. Glass body     (frosted glass, backdrop-filter)
 * 3. Highlight via ::before (specular rim)
 */
import { cn } from '@/lib/utils'

const props = defineProps<{
  class?: string
  /** Glass intensity */
  variant?: 'default' | 'strong' | 'subtle'
}>()

const variantClass =
  props.variant === 'strong' ? 'glass glass-strong' :
  props.variant === 'subtle' ? 'glass glass-subtle' :
  'glass'
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
