<script setup lang="ts">
/**
 * Dashed sidebar slot shown where the first item of a section will appear.
 * Same height as a real row, so the list does not jump once it fills.
 */
import type { Component } from 'vue'
import { Plus } from '@lucide/vue'

defineProps<{
  icon: Component
  title: string
  description: string
}>()
</script>

<template>
  <button
    type="button"
    class="group/placeholder flex min-h-11 w-full min-w-0 items-center gap-2.5 rounded-lg border border-dashed border-foreground/15 px-2 py-1.5 text-left transition-colors duration-150 hover:border-primary/40 hover:bg-sidebar-accent/50 focus-visible:outline-2 focus-visible:outline-primary"
  >
    <span
      class="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-background text-muted-foreground transition-colors duration-150 group-hover/placeholder:text-sidebar-accent-foreground"
    >
      <component :is="icon" class="size-3.5" aria-hidden="true" />
    </span>
    <span class="min-w-0 flex-1">
      <span class="block truncate text-[13px] font-medium text-foreground">{{ title }}</span>
      <span class="block text-[11px] leading-snug text-muted-foreground">{{ description }}</span>
    </span>
    <Plus
      class="size-3.5 shrink-0 text-muted-foreground opacity-0 transition-opacity duration-150 group-hover/placeholder:opacity-100 group-focus-visible/placeholder:opacity-100"
      aria-hidden="true"
    />
  </button>
</template>
