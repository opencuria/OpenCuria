<script setup lang="ts">
import { computed } from 'vue'
import { Badge } from '@/components/ui/badge'
import { Coins } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'

const props = defineProps<{
  part: HarnessPart
}>()

function formatTokens(value: unknown): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  return value.toLocaleString('en-US')
}

const tokens = computed(() => {
  const meta = props.part.meta ?? {}
  const raw = meta['tokens']
  if (raw && typeof raw === 'object') {
    const typed = raw as Record<string, unknown>
    const prompt = formatTokens(typed['prompt_tokens'])
    const completion = formatTokens(typed['completion_tokens'])
    const total = formatTokens(typed['total_tokens'])
    const shown = [prompt !== null ? `${prompt} in` : null, completion !== null ? `${completion} out` : null]
      .filter(Boolean)
      .join(' · ')
    return { detail: shown || null, total }
  }
  return { detail: null, total: null }
})

const cost = computed(() => {
  const raw = props.part.meta?.['cost']
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null
  return `$${raw.toFixed(4)}`
})

const step = computed(() => {
  const raw = props.part.meta?.['step']
  return raw === undefined || raw === null ? null : String(raw)
})
</script>

<template>
  <div class="flex max-w-3xl flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
    <Coins :size="13" class="shrink-0" />
    <span class="font-medium">Step{{ step ? ` ${step}` : '' }} finished</span>
    <Badge v-if="cost" variant="secondary">{{ cost }}</Badge>
    <Badge v-if="tokens.total" variant="outline">{{ tokens.total }} tokens</Badge>
    <span v-if="tokens.detail" class="opacity-80">{{ tokens.detail }}</span>
  </div>
</template>
