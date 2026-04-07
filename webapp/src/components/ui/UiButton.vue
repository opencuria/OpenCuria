<script setup lang="ts">
/**
 * UiButton — Apple Liquid Glass button with spring-physics press animation.
 *
 * Primary variant:  solid glass with Apple Blue fill
 * Secondary:        glass surface with border
 * Ghost:            transparent, hover reveals glass
 * Destructive:      error-tinted glass
 *
 * Spring profile: Snappy (stiffness=170, damping=20)
 */
import { computed } from 'vue'
import { cn } from '@/lib/utils'
import { cva, type VariantProps } from 'class-variance-authority'

const buttonVariants = cva(
  // Base — shared across all variants
  [
    'inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
    'disabled:pointer-events-none disabled:opacity-40 cursor-pointer',
    'position-relative overflow-hidden',
    // Spring press animation
    'transition-[transform,box-shadow,background,opacity] duration-[200ms]',
    '[transition-timing-function:var(--spring-snappy)]',
    'active:scale-[0.97] active:shadow-sm',
    // Layer promotion
    'will-change-[backdrop-filter,transform]',
    'select-none',
    // Letter spacing
    'tracking-[var(--tracking-body)]',
  ].join(' '),
  {
    variants: {
      variant: {
        default: [
          // Primary — Apple Blue glass
          'bg-primary text-primary-fg',
          'shadow-[0_2px_8px_oklch(0.55_0.20_258/0.35),inset_0_1px_0_oklch(1_0_0/0.20)]',
          'hover:bg-primary-hover hover:shadow-[0_4px_16px_oklch(0.55_0.20_258/0.45),inset_0_1px_0_oklch(1_0_0/0.22)]',
          'border border-[oklch(0.55_0.20_258/0.30)]',
        ].join(' '),

        secondary: [
          // Glass surface with border
          'glass',
          'text-fg',
          'hover:glass-strong',
          'border border-[var(--glass-border)]',
        ].join(' '),

        outline: [
          'bg-transparent text-fg',
          'border border-[var(--color-border)]',
          'hover:glass-subtle hover:border-[var(--glass-border)]',
          'hover:shadow-[var(--glass-shadow-sm)]',
        ].join(' '),

        ghost: [
          'bg-transparent text-fg border border-transparent',
          'hover:glass-subtle hover:border-[var(--glass-border-inner)]',
        ].join(' '),

        destructive: [
          'bg-error text-white',
          'shadow-[0_2px_8px_oklch(0.55_0.22_27/0.35),inset_0_1px_0_oklch(1_0_0/0.18)]',
          'hover:opacity-90 hover:shadow-[0_4px_16px_oklch(0.55_0.22_27/0.45)]',
          'border border-[oklch(0.55_0.22_27/0.30)]',
        ].join(' '),

        link: 'text-primary underline-offset-4 hover:underline bg-transparent border-transparent',
      },

      size: {
        default: 'h-10 px-[var(--sp-3)] py-[var(--sp-2)] text-[15px] rounded-[var(--radius-sm)]',
        sm:      'h-8  px-[var(--sp-2)] py-[var(--sp-1)] text-[13px] rounded-[var(--radius-xs)]',
        lg:      'h-12 px-[var(--sp-4)] py-[var(--sp-2)] text-[17px] rounded-[var(--radius-md)]',
        icon:    'h-10 w-10 rounded-[var(--radius-sm)]',
        'icon-sm': 'h-8 w-8 rounded-[var(--radius-xs)]',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
)

type ButtonVariants = VariantProps<typeof buttonVariants>

const props = withDefaults(
  defineProps<{
    variant?: NonNullable<ButtonVariants['variant']>
    size?: NonNullable<ButtonVariants['size']>
    as?: string
    disabled?: boolean
  }>(),
  {
    variant: 'default',
    size: 'default',
    as: 'button',
    disabled: false,
  },
)

const classes = computed(() =>
  cn(buttonVariants({ variant: props.variant, size: props.size })),
)
</script>

<template>
  <component :is="as" :class="classes" :disabled="disabled">
    <slot />
  </component>
</template>
