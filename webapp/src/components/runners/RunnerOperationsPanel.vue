<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import { Activity, ChevronDown, Search } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
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
defineProps<{ operations: RunnerStorage['operations']; targets?: Record<string, string> }>()
const emit = defineEmits<{ changed: [] }>()
const inspection = ref<OperationInspection | null>(null),
  busy = ref(false),
  error = ref(''),
  acknowledge = ref(false)
let version = 0
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
  <div
    v-if="operations.length || error || inspection"
    class="min-w-0 space-y-3"
    id="runner-operations"
  >
    <h3 class="flex items-center gap-2 text-sm font-semibold">
      <Activity :size="16" class="text-muted-foreground" /> Activity & intervention
    </h3>
    <p v-if="error" role="alert" class="text-sm text-destructive">{{ error }}</p>
    <div
      v-for="op in operations"
      :key="op.task_id"
      class="flex min-w-0 items-start gap-3 rounded-md border px-3 py-2"
    >
      <div class="min-w-0 flex-1 space-y-1">
        <div class="flex flex-wrap items-center gap-2">
          <span class="break-all text-sm font-medium">{{
            targets?.[op.target] || op.target || op.task_id
          }}</span>
          <Badge variant="secondary">{{ op.task__status }}</Badge>
        </div>
        <p class="break-words text-xs text-muted-foreground">
          {{ op.phase }} · Deliveries: {{ op.deliveries }}
        </p>
        <p v-if="op.task__error" class="break-words text-xs text-destructive">
          {{ op.task__error }}
        </p>
      </div>
      <Button
        size="sm"
        variant="outline"
        aria-label="Inspect current runner evidence"
        :disabled="busy"
        @click="inspect(op.task_id)"
      >
        <Search :size="14" /> Inspect
      </Button>
    </div>
    <div v-if="inspection" class="min-w-0 rounded-md border p-3 space-y-3">
      <p class="break-words text-sm">{{ inspection.diagnostic }}</p>
      <Collapsible>
        <CollapsibleTrigger as-child>
          <Button size="sm" variant="ghost"><ChevronDown :size="14" /> Execution evidence</Button>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <pre class="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{{
            JSON.stringify(inspection.evidence, null, 2)
          }}</pre>
        </CollapsibleContent>
      </Collapsible>
      <p v-if="!inspection.permitted_actions?.length" class="text-sm text-muted-foreground">
        No safe action permitted by current inspection.
      </p>
      <div class="flex flex-wrap gap-2">
        <Button
          v-for="action in inspection.permitted_actions"
          :key="action"
          size="sm"
          variant="outline"
          :disabled="busy"
          @click="action === 'acknowledge_interrupted' ? (acknowledge = true) : act(action)"
        >
          {{ action.split('_').join(' ') }}
        </Button>
      </div>
    </div>
    <Dialog :open="acknowledge" @update:open="(v) => (acknowledge = v)"
      ><DialogContent
        ><DialogHeader
          ><DialogTitle>Acknowledge interrupted execution?</DialogTitle
          ><DialogDescription
            >Resources are preserved, not deleted or recreated. This releases the retained operation
            fence only after the server revalidates fresh exclusive-process evidence. No automatic
            restart or credential scrub is implied.</DialogDescription
          ></DialogHeader
        ><DialogFooter
          ><Button variant="outline" @click="acknowledge = false">Cancel</Button
          ><Button :disabled="busy" @click="act('acknowledge_interrupted')"
            >Acknowledge, preserving resources</Button
          ></DialogFooter
        ></DialogContent
      ></Dialog
    >
  </div>
</template>
