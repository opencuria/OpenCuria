<script setup lang="ts">
import { computed, useAttrs } from 'vue'
import type { ClassValue } from 'clsx'
import { cn } from '@/lib/utils'

defineOptions({ inheritAttrs: false })

withDefaults(
  defineProps<{
    iconOnly?: boolean
    alt?: string
  }>(),
  {
    iconOnly: false,
    alt: 'OpenCuria logo',
  },
)

const attrs = useAttrs()

const svgAttrs = computed(() => {
  const rest = { ...attrs }
  delete rest.class
  return rest
})

const iconClass = computed(() => cn('block size-8', attrs.class as ClassValue))
const wordmarkClass = computed(() => cn('block h-10 w-auto', attrs.class as ClassValue))
</script>

<template>
  <svg
    v-if="iconOnly"
    v-bind="svgAttrs"
    viewBox="13 13 38 38"
    :class="iconClass"
    style="color: var(--color-foreground)"
    role="img"
    :aria-label="alt"
  >
    <polygon points="18,23 32,15 32,32" fill="currentColor" opacity="0.24" />
    <polygon points="32,15 46,23 32,32" fill="currentColor" opacity="0.42" />
    <polygon points="46,23 32,32 46,41" fill="var(--color-primary)" />
    <polygon points="32,32 32,49 46,41" fill="var(--color-primary-hover)" />
    <polygon points="18,23 32,32 18,41" fill="currentColor" opacity="0.18" />
    <polygon points="32,32 32,49 18,41" fill="currentColor" opacity="0.1" />
    <polygon points="18,23 32,15 46,23 46,41 32,49 18,41" fill="none" stroke="currentColor" stroke-opacity="0.22" stroke-width="1.2" />
    <line x1="18" y1="23" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="15" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="46" y1="23" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="32" x2="46" y2="41" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="32" x2="32" y2="49" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="32" x2="18" y2="41" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
  </svg>

  <svg
    v-else
    v-bind="svgAttrs"
    viewBox="0 0 244 64"
    :class="wordmarkClass"
    style="color: var(--color-foreground)"
    role="img"
    :aria-label="alt"
  >
    <polygon points="18,23 32,15 32,32" fill="currentColor" opacity="0.24" />
    <polygon points="32,15 46,23 32,32" fill="currentColor" opacity="0.42" />
    <polygon points="46,23 32,32 46,41" fill="var(--color-primary)" />
    <polygon points="32,32 32,49 46,41" fill="var(--color-primary-hover)" />
    <polygon points="18,23 32,32 18,41" fill="currentColor" opacity="0.18" />
    <polygon points="32,32 32,49 18,41" fill="currentColor" opacity="0.1" />
    <polygon points="18,23 32,15 46,23 46,41 32,49 18,41" fill="none" stroke="currentColor" stroke-opacity="0.22" stroke-width="1.2" />
    <line x1="18" y1="23" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="15" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="46" y1="23" x2="32" y2="32" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="32" x2="46" y2="41" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="32" x2="32" y2="49" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <line x1="32" y1="32" x2="18" y2="41" stroke="currentColor" stroke-opacity="0.22" stroke-width="1" />
    <text x="78" y="42" font-family="SF Pro Display, Inter, Segoe UI, sans-serif" font-size="30" font-weight="600" fill="var(--color-foreground)" letter-spacing="-0.6">
      Open<tspan font-weight="400" fill="var(--color-primary)">Curia</tspan>
    </text>
  </svg>
</template>
