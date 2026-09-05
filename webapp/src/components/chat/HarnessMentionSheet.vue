<script setup lang="ts">
import type { MentionCandidate } from '@/lib/harnessMentions'

defineProps<{
  candidates: MentionCandidate[]
  activeIndex: number
}>()

const emit = defineEmits<{
  select: [candidate: MentionCandidate]
  hover: [index: number]
}>()
</script>

<template>
  <div
    class="px-2 pb-2 pt-1"
    role="listbox"
    aria-label="Mention suggestions"
    data-testid="composer-mention-sheet"
  >
    <div class="max-h-48 overflow-y-auto py-1">
      <button
        v-for="(candidate, idx) in candidates"
        :key="`${candidate.kind}:${candidate.insert}`"
        type="button"
        role="option"
        :aria-selected="idx === activeIndex"
        class="flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-xs transition-colors"
        :class="
          idx === activeIndex
            ? 'bg-muted text-foreground'
            : 'text-muted-foreground hover:text-foreground'
        "
        data-testid="composer-mention-option"
        @mousedown.prevent="emit('select', candidate)"
        @mousemove="emit('hover', idx)"
      >
        <span
          class="rounded px-1 py-0.5 text-[10px] font-medium"
          :class="
            candidate.kind === 'agent' || candidate.kind === 'skill'
              ? 'bg-primary/10 text-primary'
              : 'bg-muted text-muted-foreground'
          "
        >
          {{ candidate.kind }}
        </span>
        <span class="min-w-0 flex-1 truncate">{{ candidate.label }}</span>
      </button>
    </div>
  </div>
</template>
