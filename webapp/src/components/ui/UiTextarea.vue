<script setup lang="ts">
import { cn } from '@/lib/utils'
import { useTextareaAutosize } from '@vueuse/core'
import { computed, ref, toRef } from 'vue'

const props = defineProps<{
  modelValue?: string
  placeholder?: string
  disabled?: boolean
  rows?: number
  class?: string
  autosize?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
  keydown: [event: KeyboardEvent]
}>()

const textarea = ref<HTMLTextAreaElement | null>(null)

// If autosize is enabled, we use vueuse's useTextareaAutosize
// using a computed for two-way binding with the prop
if (props.autosize) {
  useTextareaAutosize({
    element: textarea,
    input: computed({
      get: () => props.modelValue ?? '',
      set: (val: string) => emit('update:modelValue', val),
    }),
  })
}
</script>

<template>
  <textarea
    ref="textarea"
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :rows="rows ?? 3"
    :class="
      cn(
        'flex w-full text-[15px] text-fg placeholder:text-muted-fg resize-none',
        'px-[var(--sp-3)] py-[var(--sp-2)]',
        'transition-[box-shadow,border-color] duration-[200ms]',
        'focus:outline-none focus:border-[var(--color-primary)] focus:shadow-[0_0_0_3px_oklch(0.55_0.20_258/0.20)]',
        'disabled:cursor-not-allowed disabled:opacity-40',
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
    @input="$emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    @keydown="$emit('keydown', $event)"
  />
</template>
