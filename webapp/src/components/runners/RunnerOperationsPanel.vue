<script setup lang="ts">
import { ref } from 'vue'
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
defineProps<{ operations: RunnerStorage['operations'] }>()
const emit = defineEmits<{ changed: [] }>()
const inspection = ref<OperationInspection | null>(null),
  busy = ref(false),
  error = ref(''),
  acknowledge = ref(false)
async function inspect(id: string) {
  inspection.value = null
  busy.value = true
  error.value = ''
  try {
    inspection.value = await api.inspectOperation(id)
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Inspection failed'
  } finally {
    busy.value = false
  }
}
async function act(action: DispositionAction) {
  if (!inspection.value?.permitted_actions.includes(action)) return
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
  <div class="space-y-3" id="runner-operations">
    <h3 class="font-semibold">Operations & intervention</h3>
    <p v-if="error" role="alert" class="text-destructive">{{ error }}</p>
    <div v-for="op in operations" :key="op.task_id" class="rounded-md border p-3 space-y-2">
      <p class="break-all">{{ op.task_id }} · {{ op.phase }} · {{ op.task__status }}</p>
      <p>
        {{ op.task__error || 'Waiting for execution evidence' }} · Deliveries: {{ op.deliveries }}
      </p>
      <Button variant="outline" :disabled="busy" @click="inspect(op.task_id)"
        >Inspect current runner evidence</Button
      >
    </div>
    <div v-if="inspection" class="rounded-md border p-3 space-y-3">
      <p>{{ inspection.diagnostic }}</p>
      <pre class="max-h-64 overflow-auto whitespace-pre-wrap text-xs">{{
        JSON.stringify(inspection.evidence, null, 2)
      }}</pre>
      <p v-if="!inspection.permitted_actions?.length">
        No safe action permitted by current inspection.
      </p>
      <Button
        v-for="action in inspection.permitted_actions"
        :key="action"
        variant="outline"
        :disabled="busy"
        @click="action === 'acknowledge_interrupted' ? (acknowledge = true) : act(action)"
        >{{ action.split('_').join(' ') }}</Button
      >
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
