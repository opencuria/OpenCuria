<script setup lang="ts">
import { cn } from '@/lib/utils'

defineProps<{
  modelValue?: string
  disabled?: boolean
  class?: string
  options: { value: string; label: string; disabled?: boolean }[]
  placeholder?: string
}>()

defineEmits<{
  'update:modelValue': [value: string]
}>()
</script>

<template>
  <select
    :value="modelValue"
    :disabled="disabled"
    :class="
      cn(
        'flex h-10 w-full appearance-none text-[15px] text-fg px-[var(--sp-3)] py-[var(--sp-2)] pr-8',
        'transition-[box-shadow,border-color] duration-[200ms]',
        'focus:outline-none',
        'focus:border-[var(--color-primary)] focus:shadow-[0_0_0_3px_oklch(0.55_0.20_258/0.20)]',
        'disabled:cursor-not-allowed disabled:opacity-40',
        'cursor-pointer',
        $props.class,
      )
    "
    style="
      border-radius: var(--radius-sm);
      background: var(--input-bg);
      border: 1px solid var(--input-border);
      letter-spacing: var(--tracking-body);
      backdrop-filter: blur(4px);
      -webkit-backdrop-filter: blur(4px);
    "
    @change="$emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
  >
    <option v-if="placeholder" value="" disabled>{{ placeholder }}</option>
    <option
      v-for="opt in options"
      :key="opt.value"
      :value="opt.value"
      :disabled="opt.disabled"
    >
      {{ opt.label }}
    </option>
  </select>
</template>
