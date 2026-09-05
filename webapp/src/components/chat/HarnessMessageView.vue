<script setup lang="ts">
import { computed } from 'vue'
import { User } from '@lucide/vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import type { HarnessMessage, HarnessPart } from '@/types/harness'
import HarnessMarkdown from './HarnessMarkdown.vue'
import HarnessReasoning from './HarnessReasoning.vue'
import HarnessToolCard from './HarnessToolCard.vue'
import HarnessStepFinish from './HarnessStepFinish.vue'
import HarnessSubtaskCard from './HarnessSubtaskCard.vue'
import HarnessPatchCard from './HarnessPatchCard.vue'

const props = defineProps<{
  message: HarnessMessage
  streaming?: boolean
  childSessionIds?: Record<string, string>
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
}>()

const orderedParts = computed<HarnessPart[]>(() => [...props.message.parts])

const textParts = computed(() => orderedParts.value.filter((p) => p.type === 'text'))
const nonTextParts = computed(() =>
  orderedParts.value.filter((p) => p.type !== 'text'),
)

const combinedText = computed(() => textParts.value.map((p) => p.output).join(''))

function childIdFor(part: HarnessPart): string | null {
  const fromMeta = part.meta?.['child_session_id']
  if (typeof fromMeta === 'string' && fromMeta) return fromMeta
  const fromMap = props.childSessionIds?.[String(part.meta?.['subtask_id'] ?? '')]
  return fromMap ?? null
}
</script>

<template>
  <!-- User message -->
  <div v-if="message.role === 'user'" class="flex items-start gap-3 justify-end">
    <div class="min-w-0 max-w-3xl">
      <div
        class="overflow-x-auto rounded-[var(--radius-md)] rounded-br-sm bg-primary text-primary-foreground px-4 py-3 text-sm break-words"
      >
        <HarnessMarkdown :text="message.content" compact />
      </div>
    </div>
    <div class="flex items-center justify-center w-8 h-8 rounded-full bg-primary/10 text-primary shrink-0">
      <User :size="14" />
    </div>
  </div>

  <!-- Assistant message: block model in left prose shell -->
  <div v-else class="flex items-start gap-3">
    <div class="min-w-0 flex-1 max-w-3xl py-2 text-sm text-foreground">
      <div v-if="combinedText">
        <HarnessMarkdown :text="combinedText" />
      </div>
      <div v-if="streaming && !combinedText && nonTextParts.length === 0" class="flex items-center gap-2 text-muted-foreground">
        <LoadingSpinner :size="14" />
        <span class="text-xs">Agent is thinking…</span>
      </div>
      <span
        v-if="streaming && combinedText"
        class="ml-0.5 inline-block h-4 w-2 animate-pulse bg-primary/60 align-middle"
      />
      <div class="mt-2 flex flex-col gap-2">
        <template v-for="part in nonTextParts" :key="part.id">
          <HarnessReasoning v-if="part.type === 'reasoning'" :part="part" />
          <HarnessToolCard v-else-if="part.type === 'tool'" :part="part" />
          <HarnessStepFinish v-else-if="part.type === 'step-finish'" :part="part" />
          <HarnessSubtaskCard
            v-else-if="part.type === 'subtask'"
            :part="part"
            :child-session-id="childIdFor(part)"
            @open-subtask="emit('openSubtask', $event)"
          />
          <HarnessPatchCard v-else-if="part.type === 'patch'" :part="part" />
          <div
            v-else-if="part.type === 'agent'"
            class="w-full overflow-x-auto rounded-xl border border-border bg-card px-3 py-2"
          >
            <p class="text-xs font-medium text-muted-foreground">{{ part.title || part.type }}</p>
            <pre class="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-muted-foreground">{{ part.output }}</pre>
          </div>
        </template>
      </div>
      <p v-if="message.error" class="mt-2 text-xs text-destructive">{{ message.error }}</p>
    </div>
  </div>
</template>
