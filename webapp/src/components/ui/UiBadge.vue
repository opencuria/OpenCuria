<script setup lang="ts">
/**
 * UiBadge — Liquid Glass pill badge.
 * Uses glass-subtle background with OKLCH status tints.
 */
import { computed } from 'vue'
import { cn } from '@/lib/utils'
import { cva, type VariantProps } from 'class-variance-authority'

const badgeVariants = cva(
  [
    'inline-flex items-center gap-1',
    'rounded-[var(--radius-full)]',
    'px-[10px] py-[3px]',
    'text-[12px] font-medium',
    'border',
    'transition-[background,border-color,color] duration-[200ms]',
    'letter-spacing-[var(--tracking-caption)]',
  ].join(' '),
  {
    variants: {
      variant: {
        default:     'bg-[oklch(0.55_0.20_258/0.12)] text-primary border-[oklch(0.55_0.20_258/0.20)]',
        success:     'bg-[oklch(0.52_0.16_150/0.12)] text-success border-[oklch(0.52_0.16_150/0.20)]',
        warning:     'bg-[oklch(0.65_0.18_80/0.12)]  text-warning border-[oklch(0.65_0.18_80/0.20)]',
        error:       'bg-[oklch(0.55_0.22_27/0.12)]  text-error   border-[oklch(0.55_0.22_27/0.20)]',
        info:        'bg-[oklch(0.58_0.18_248/0.12)] text-info    border-[oklch(0.58_0.18_248/0.20)]',
        muted:       'bg-[var(--glass-bg-subtle)] text-muted-fg border-[var(--glass-border)]',
        outline:     'bg-transparent text-fg border-[var(--color-border)]',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  },
)

type BadgeVariants = VariantProps<typeof badgeVariants>

const props = withDefaults(
  defineProps<{
    variant?: NonNullable<BadgeVariants['variant']>
  }>(),
  { variant: 'default' },
)

const classes = computed(() => cn(badgeVariants({ variant: props.variant })))
</script>

<template>
  <span :class="classes">
    <slot />
  </span>
</template>
