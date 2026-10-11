<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { ChevronDown, Search } from '@lucide/vue'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
import * as api from '@/services/runnerStorage.api'
import type { RunnerStorage, OperationInspection, DispositionAction } from '@/types/runnerStorage'
defineProps<{ operations: RunnerStorage['operations']; diagnostic?: string }>()
const emit = defineEmits<{ changed: [] }>()
const inspection = ref<OperationInspection | null>(null),
  busy = ref(false),
  error = ref(''),
  acknowledge = ref(false)
let version = 0
const actionLabels: Record<DispositionAction, string> = {
  reconcile: 'Apply confirmed result',
  retry: 'Try again',
  acknowledge_interrupted: 'Close interrupted operation',
}
const reviewMessage = computed(
  () =>
    inspection.value?.review_message ||
    'Status could not be verified. Check the runner connection and see Details.',
)
onUnmounted(() => {
  version++
})
async function inspect(id: string) {
  if (busy.value) return
  const requestVersion = ++version
  inspection.value = null
  busy.value = true
  error.value = ''
  try {
    const result = await api.inspectOperation(id)
    if (requestVersion === version) inspection.value = result
  } catch (e) {
    if (requestVersion === version)
      error.value = e instanceof Error ? e.message : 'Inspection failed'
  } finally {
    if (requestVersion === version) busy.value = false
  }
}
async function act(action: DispositionAction) {
  if (busy.value || !inspection.value?.permitted_actions.includes(action)) return
  busy.value = true
  error.value = ''
  try {
    await api.disposeOperation(inspection.value.operation_id, action)
    inspection.value = null
    acknowledge.value = false
    emit('changed')
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Action refused; inspect again'
    inspection.value = null
    acknowledge.value = false
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div v-if="operations.length || error || inspection" class="min-w-0 space-y-3">
    <p v-if="error" role="alert" class="text-sm text-destructive">{{ error }}</p>
    <div class="flex flex-wrap gap-2">
      <Button
        v-for="(op, index) in operations"
        :key="op.task_id"
        size="sm"
        variant="outline"
        :disabled="busy"
        @click="inspect(op.task_id)"
      >
        <Search :size="14" />
        {{
          busy
            ? 'Checking…'
            : operations.length > 1
              ? `Check ${op.task__workspace__name || `operation ${index + 1}`}`
              : 'Check status'
        }}
      </Button>
    </div>
    <div v-if="inspection" class="min-w-0 rounded-md border p-3 space-y-3">
      <p class="break-words text-sm">{{ reviewMessage }}</p>
      <div class="flex flex-wrap gap-2">
        <Button
          v-for="action in inspection.permitted_actions"
          :key="action"
          size="sm"
          variant="outline"
          :disabled="busy"
          @click="action === 'acknowledge_interrupted' ? (acknowledge = true) : act(action)"
        >
          {{ actionLabels[action] }}
        </Button>
      </div>
    </div>
    <Collapsible v-if="diagnostic || inspection">
      <CollapsibleTrigger as-child>
        <Button size="sm" variant="ghost"><ChevronDown :size="14" /> Details</Button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <p
          v-if="diagnostic || inspection?.diagnostic"
          class="break-words text-xs text-muted-foreground"
        >
          {{ diagnostic || inspection?.diagnostic }}
        </p>
        <pre
          v-if="inspection"
          class="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs"
          >{{ JSON.stringify(inspection.evidence, null, 2) }}</pre
        >
      </CollapsibleContent>
    </Collapsible>
    <Dialog :open="acknowledge" @update:open="(v) => (acknowledge = v)"
      ><DialogContent
        ><DialogHeader
          ><DialogTitle>Close interrupted operation?</DialogTitle
          ><DialogDescription
            >This releases the block on further changes and keeps existing resources. The workspace
            will not be restarted or repaired.</DialogDescription
          ></DialogHeader
        ><DialogFooter
          ><Button variant="outline" @click="acknowledge = false">Cancel</Button
          ><Button :disabled="busy" @click="act('acknowledge_interrupted')"
            >Close operation and keep resources</Button
          ></DialogFooter
        ></DialogContent
      ></Dialog
    >
  </div>
</template>
