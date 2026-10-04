<script setup lang="ts">
import { computed } from 'vue'
import { Camera, ChevronDown, Trash2, TriangleAlert } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import RunnerOperationsPanel from './RunnerOperationsPanel.vue'
import type { RunnerStorage, DeletionRequest, DeletionTarget } from '@/types/runnerStorage'

const props = defineProps<{ data: RunnerStorage; deletions: DeletionRequest[]; busy?: boolean }>()
const emit = defineEmits<{
  changed: []
  cancel: [id: string]
  review: [target: DeletionTarget, name: string]
}>()
const shortId = (id: string) => (id.length > 18 ? `${id.slice(0, 12)}…${id.slice(-4)}` : id)
const names = computed(() => {
  const result: Record<string, string> = {}
  for (const runtime of props.data.runtimes) {
    for (const resource of runtime.resources) {
      if (resource.workspace) result[resource.workspace.id] = resource.workspace.name
    }
  }
  for (const generation of props.data.generations) {
    result[generation.id] = generation.name
    if (generation.definition_id && generation.definition_name)
      result[generation.definition_id] = generation.definition_name
    for (const workspace of generation.dependencies) result[workspace.id] = workspace.name
  }
  for (const request of props.deletions) {
    for (const item of [...request.approval.images, ...request.approval.workspaces]) {
      if (!result[item.id]) result[item.id] = item.name
    }
  }
  return result
})
const label = (id: string) => names.value[id] || shortId(id)
const targets = computed(() =>
  Object.fromEntries(
    props.data.operations.map((op) => [op.target, label(op.target || op.task_id)]),
  ),
)
const terminal = (phase: string) => ['completed', 'cancelled', 'failed'].includes(phase)
const outcome = (phase: string) =>
  phase === 'cancelled'
    ? 'Cancelled — resources retained'
    : phase === 'completed'
      ? 'Completed'
      : phase.split('_').join(' ')
const sortedDeletions = computed(() =>
  [...props.deletions].sort((a, b) => Number(terminal(a.phase)) - Number(terminal(b.phase))),
)
const captureNeedsReview = (capture: RunnerStorage['capture_requests'][number]) =>
  capture.resume_suppressed || ['failed', 'intervention_required'].includes(capture.phase)
const captureGroups = computed(() => [
  {
    history: false,
    entries: props.data.capture_requests.filter((c) => !terminal(c.phase) || captureNeedsReview(c)),
  },
  {
    history: true,
    entries: props.data.capture_requests.filter((c) => terminal(c.phase) && !captureNeedsReview(c)),
  },
])
const hasActivity = computed(
  () =>
    props.data.operations.length || props.deletions.length || props.data.capture_requests.length,
)
function cancel(request: DeletionRequest) {
  if (request.can_cancel && !props.busy) emit('cancel', request.id)
}
function review(request: DeletionRequest) {
  if (!props.busy && request.phase === 'reconfirmation_required')
    emit(
      'review',
      { target_type: request.target_type, target_id: request.target_id },
      label(request.target_id),
    )
}
</script>

<template>
  <div class="min-w-0 space-y-4">
    <p v-if="!hasActivity" class="text-sm text-muted-foreground">No recorded activity</p>
    <RunnerOperationsPanel
      :operations="data.operations"
      :targets="targets"
      @changed="emit('changed')"
    />
    <section v-if="deletions.length" class="min-w-0 space-y-2" aria-label="Deletion requests">
      <h3 class="flex items-center gap-2 text-sm font-semibold">
        <Trash2 :size="16" class="text-muted-foreground" /> Deletion requests
      </h3>
      <div class="divide-y rounded-md border">
        <div
          v-for="request in sortedDeletions"
          :key="request.id"
          class="min-w-0 space-y-1.5 px-3 py-2"
        >
          <div class="flex flex-wrap items-center gap-2">
            <span class="break-words text-sm font-medium">{{ label(request.target_id) }}</span>
            <Badge variant="secondary">{{ outcome(request.phase) }}</Badge>
            <span class="text-xs text-muted-foreground"
              >{{ request.target_type }} · {{ request.mode }}</span
            >
          </div>
          <p
            v-if="request.diagnostic"
            role="alert"
            class="break-words text-xs text-muted-foreground"
          >
            {{ request.diagnostic }}
          </p>
          <div
            v-if="request.can_cancel || request.phase === 'reconfirmation_required'"
            class="flex flex-wrap gap-2"
          >
            <Button
              v-if="request.can_cancel"
              size="sm"
              variant="outline"
              :disabled="busy"
              @click="cancel(request)"
              >Cancel pending request</Button
            >
            <Button
              v-if="request.phase === 'reconfirmation_required'"
              size="sm"
              variant="outline"
              :disabled="busy"
              @click="review(request)"
              >Review changed graph and confirm again</Button
            >
          </div>
        </div>
      </div>
    </section>
    <section
      v-if="data.capture_requests.length"
      class="min-w-0 space-y-2"
      aria-label="Capture pipeline"
    >
      <h3 class="flex items-center gap-2 text-sm font-semibold">
        <Camera :size="16" class="text-muted-foreground" /> Capture pipeline
      </h3>
      <template v-for="group in captureGroups" :key="String(group.history)">
        <Collapsible v-if="group.entries.length" :open="group.history ? undefined : true">
          <CollapsibleTrigger v-if="group.history" as-child>
            <Button size="sm" variant="ghost"
              ><ChevronDown :size="14" /> Completed history ({{ group.entries.length }})</Button
            >
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div class="divide-y rounded-md border">
              <div
                v-for="capture in group.entries"
                :key="capture.id"
                class="min-w-0 space-y-1.5 px-3 py-2"
              >
                <div class="flex flex-wrap items-center gap-2">
                  <span class="break-words text-sm font-medium"
                    >{{ label(capture.workspace_id) }} → {{ label(capture.image_id) }}</span
                  >
                  <Badge variant="secondary">{{ outcome(capture.phase) }}</Badge>
                </div>
                <p
                  v-if="capture.diagnostic"
                  role="alert"
                  class="break-words text-xs text-muted-foreground"
                >
                  {{ capture.diagnostic }}
                </p>
                <p
                  v-if="capture.resume_suppressed"
                  role="alert"
                  class="flex items-center gap-1.5 text-xs text-warning"
                >
                  <TriangleAlert :size="14" class="shrink-0" /> Restart suppressed
                </p>
              </div>
            </div>
          </CollapsibleContent>
        </Collapsible>
      </template>
    </section>
    <Collapsible v-if="hasActivity">
      <CollapsibleTrigger as-child
        ><Button size="sm" variant="ghost"
          ><ChevronDown :size="14" /> Technical identifiers</Button
        ></CollapsibleTrigger
      >
      <CollapsibleContent>
        <pre
          class="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs text-muted-foreground"
          >{{
            JSON.stringify(
              {
                operations: data.operations,
                deletions: deletions.map((r) => ({
                  id: r.id,
                  target_type: r.target_type,
                  target_id: r.target_id,
                })),
                captures: data.capture_requests,
              },
              null,
              2,
            )
          }}</pre
        >
      </CollapsibleContent>
    </Collapsible>
  </div>
</template>
