<script setup lang="ts">
/**
 * UiInput — Liquid Glass input field.
 * Glass surface with focus glow (Apple Blue ring).
 */
import { cn } from '@/lib/utils'

defineProps<{
  modelValue?: string
  placeholder?: string
  disabled?: boolean
  type?: string
  class?: string
}>()

defineEmits<{
  'update:modelValue': [value: string]
}>()
</script>

<template>
  <input
    :type="type ?? 'text'"
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :class="
      cn(
        'flex h-10 w-full',
        'rounded-[var(--radius-sm)]',
        'border border-[var(--input-border)]',
        'px-[var(--sp-3)] py-[var(--sp-2)]',
        'text-[15px] text-fg placeholder:text-muted-fg',
        'letter-spacing-[var(--tracking-body)]',
        // Glass effect
        'bg-[var(--input-bg)]',
        'backdrop-blur-sm',
        // Transitions
        'transition-[box-shadow,border-color,background] duration-[200ms]',
        '[transition-timing-function:var(--spring-snappy)]',
        // Focus ring — Apple Blue glow
        'focus:outline-none',
        'focus:border-[var(--color-primary)]',
        'focus:shadow-[0_0_0_3px_oklch(0.55_0.20_258/0.20),inset_0_1px_0_oklch(1_0_0/0.12)]',
        // States
        'disabled:cursor-not-allowed disabled:opacity-40',
        'will-change-[box-shadow]',
        $props.class,
      )
    "
    @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
  />
</template>
