<script setup lang="ts">
import { computed } from 'vue'
import { Loader2 } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import { useHarnessStore } from '@/stores/harness'
import {
  formatSubagentType,
  subtaskActivityLabel,
} from '@/lib/harnessSubtaskActivity'

const props = defineProps<{
  part: HarnessPart
  childSessionId?: string | null
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
}>()

const harness = useHarnessStore()

/** The child session id links parent/child (backend `parent` FK). */
const childId = computed(() => {
  const fromMeta = props.part.meta?.['child_session_id']
  if (typeof fromMeta === 'string' && fromMeta) return fromMeta
  return props.childSessionId ?? null
})

const agentLabel = computed(() => {
  const raw = props.part.meta?.['agent']
  return formatSubagentType(typeof raw === 'string' ? raw : null)
})

const childMessages = computed(() => {
  if (!childId.value) return []
  return harness.messagesBySession[childId.value] ?? []
})

const activity = computed(() =>
  subtaskActivityLabel(props.part, childMessages.value),
)

const isRunning = computed(() => props.part.state === 'running')

function handleOpen(): void {
  if (childId.value) emit('openSubtask', childId.value)
}
</script>

<template>
  <button
    type="button"
    data-testid="harness-subtask-row"
    class="flex w-full min-w-0 items-start gap-2 py-0.5 text-left disabled:cursor-default"
    :disabled="!childId"
    @click="handleOpen"
  >
    <span
      data-testid="harness-subtask-indicator"
      :data-running="isRunning ? '1' : '0'"
      class="mt-1.5 flex h-3 w-3 shrink-0 items-center justify-center"
    >
      <Loader2
        v-if="isRunning"
        :size="12"
        class="text-muted-foreground motion-reduce:hidden motion-safe:animate-spin"
        aria-hidden="true"
      />
      <span
        class="h-1.5 w-1.5 rounded-full bg-muted-foreground"
        :class="isRunning ? 'hidden motion-reduce:inline-block' : ''"
      />
    </span>
    <span class="min-w-0 flex-1">
      <span class="flex min-w-0 items-baseline gap-1.5">
        <span class="truncate text-sm font-medium text-foreground">
          {{ part.title || 'Subagent task' }}
        </span>
        <span
          v-if="agentLabel"
          data-testid="harness-subtask-type"
          class="shrink-0 text-xs font-normal text-muted-foreground"
        >
          {{ agentLabel }}
        </span>
      </span>
      <span
        v-if="activity"
        data-testid="harness-subtask-activity"
        class="mt-0.5 block truncate text-xs text-muted-foreground"
      >
        {{ activity }}
      </span>
    </span>
  </button>
</template>
