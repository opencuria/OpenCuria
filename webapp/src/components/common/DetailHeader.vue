<!--
  DetailHeader — standard header for detail views opened from a list
  (e.g. runner or plugin detail): back link, icon, title, meta line, actions.

  Slots: `meta` (inline facts under the title), `actions` (right-aligned controls).
-->
<script setup lang="ts">
import type { Component } from 'vue'
import { ArrowLeft } from '@lucide/vue'
import { Button } from '@/components/ui/button'

defineProps<{
  title: string
  backLabel: string
  icon?: Component
  description?: string
}>()
const emit = defineEmits<{ back: [] }>()
</script>

<template>
  <header class="space-y-3">
    <Button
      variant="ghost"
      size="sm"
      class="-ml-2 text-xs text-muted-foreground"
      data-testid="detail-back"
      @click="emit('back')"
      ><ArrowLeft />{{ backLabel }}</Button
    >
    <div class="flex min-w-0 flex-wrap items-start justify-between gap-3">
      <div class="flex min-w-0 flex-1 basis-64 items-center gap-3">
        <div
          v-if="icon"
          class="flex size-11 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary"
        >
          <component :is="icon" :size="21" />
        </div>
        <div class="min-w-0 space-y-1">
          <h2 class="truncate text-xl font-semibold tracking-tight">{{ title }}</h2>
          <div
            v-if="$slots.meta"
            class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
          >
            <slot name="meta" />
          </div>
        </div>
      </div>
      <div v-if="$slots.actions" class="flex min-h-8 flex-wrap items-center gap-2">
        <slot name="actions" />
      </div>
    </div>
    <p v-if="description" class="whitespace-pre-wrap text-sm text-muted-foreground">
      {{ description }}
    </p>
  </header>
</template>
