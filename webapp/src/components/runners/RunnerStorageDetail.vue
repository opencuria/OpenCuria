<script setup lang="ts">
import { computed, toRef, ref } from 'vue'
import type { Runner } from '@/types'
import type { DeletionTarget } from '@/types/runnerStorage'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { useRunnerStorage, storageBytes } from '@/composables/useRunnerStorage'
import { cancelDeletion } from '@/services/runnerStorage.api'
import { updateRunnerImageBuild } from '@/services/workspaces.api'
import ImageDeletionDialog from '@/components/images/ImageDeletionDialog.vue'
import RunnerOperationsPanel from './RunnerOperationsPanel.vue'
const props = defineProps<{ runner: Runner }>()
const emit = defineEmits<{ back: [] }>()
const id = computed(() => props.runner.id)
const { data, deletions, error, loading, refreshPending, load, refresh } = useRunnerStorage(
  toRef(id),
)
const target = ref<DeletionTarget | null>(null)
const actionError = ref('')
const showDeleted = ref(false)
const stored = computed(() => data.value?.generations.filter((i) => i.status !== 'deleted') ?? [])
const deleted = computed(() => data.value?.generations.filter((i) => i.status === 'deleted') ?? [])
function currentDefault(image: {
  is_current: boolean
  status: string
  assignment_status?: string | null
}) {
  return (
    image.is_current &&
    image.status !== 'deleted' &&
    !['deleted', 'retired', 'deactivated'].includes(image.assignment_status ?? '')
  )
}
const groups = computed(() => [
  {
    name: 'Base / build images',
    images: stored.value.filter((i) => i.origin_type !== 'workspace_capture'),
  },
  {
    name: 'Captured images',
    images: stored.value.filter((i) => i.origin_type === 'workspace_capture'),
  },
  ...(showDeleted.value ? [{ name: 'Deleted history', images: deleted.value }] : []),
])
async function cancel(id: string) {
  actionError.value = ''
  try {
    await cancelDeletion(id)
    await load()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : 'Cancellation refused'
  }
}
async function rebuild(definition: string) {
  actionError.value = ''
  try {
    await updateRunnerImageBuild(definition, props.runner.id, { action: 'rebuild' })
    await load()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : 'Rebuild refused'
  }
}
</script>
<template>
  <div class="space-y-5 text-sm">
    <div class="flex flex-wrap gap-2 items-center">
      <Button variant="outline" @click="emit('back')">Back to runners</Button>
      <h2 class="text-lg font-semibold">{{ runner.name || runner.id }} · Storage & history</h2>
      <Button variant="outline" :disabled="refreshPending" @click="refresh">{{
        refreshPending ? 'Scan queued — awaiting new complete evidence' : 'Request fresh inventory'
      }}</Button>
    </div>
    <p v-if="error || actionError" role="alert" class="text-destructive">
      {{ error || actionError }} <Button variant="outline" @click="load">Retry inspection</Button>
    </p>
    <p v-if="loading && !data" role="status">Loading last confirmed inventory…</p>
    <template v-if="data">
      <Card
        ><CardContent class="pt-4"
          ><p role="status">
            {{
              !data.runner_online
                ? 'Runner offline — last confirmed data only.'
                : !data.latest_complete
                  ? 'Partial or unavailable inventory — absence is not proven.'
                  : data.runtimes.some((r) => !r.fresh)
                    ? 'Stale inventory — last confirmed data only.'
                    : 'Fresh cached inventory — not live.'
            }}
            Snapshot {{ data.latest_snapshot_id ?? 'unknown' }}. Lifecycle intent and observed state
            are independent.
          </p></CardContent
        ></Card
      >
      <Card v-for="runtime in data.runtimes" :key="runtime.runtime_type"
        ><CardHeader
          ><CardTitle>{{ runtime.runtime_type }} filesystem usage</CardTitle
          ><CardDescription
            >Actual filesystem use is separate from managed logical image sizes. Docker layers
            overlap; logical/shared bytes must not be summed.</CardDescription
          ></CardHeader
        ><CardContent class="space-y-3">
          <p>
            Collected {{ runtime.collected_at ?? 'unknown' }} · Received
            {{ runtime.received_at ?? 'unknown' }} ·
            {{ runtime.fresh ? 'fresh' : 'stale / unknown' }}
          </p>
          <p>
            Managed logical accounting:
            {{ runtime.resources.filter((r) => r.managed && r.logical_bytes != null).length }}
            measured resources;
            {{ runtime.resources.filter((r) => r.managed && r.logical_bytes == null).length }}
            unknown sizes.
            {{
              runtime.runtime_type === 'qemu'
                ? storageBytes(
                    runtime.resources
                      .filter((r) => r.managed)
                      .reduce((total, r) => total + (r.logical_bytes ?? 0), 0),
                  ) + ' known logical bytes (not filesystem usage)'
                : 'Overlapping logical sizes — no additive total'
            }}
          </p>
          <p>
            Unknown / foreign resources:
            {{ runtime.diagnostics?.foreign_resource_count ?? 'unknown' }} · Latest scan
            {{ runtime.diagnostics?.complete ? 'complete' : 'partial / unavailable' }}
          </p>
          <p v-for="message in runtime.diagnostics?.errors" :key="message" class="text-destructive">
            {{ message }}
          </p>
          <p v-for="fs in runtime.filesystems" :key="fs.path" class="break-all">
            {{ fs.path }}: Used {{ storageBytes(fs.used_bytes) }} / Capacity
            {{ storageBytes(fs.capacity_bytes) }} · Available {{ storageBytes(fs.available_bytes) }}
          </p>
          <Collapsible
            ><CollapsibleTrigger as-child
              ><Button variant="outline"
                >Inspect resources, caches, workspace disks & temporary files ({{
                  runtime.resources.length
                }})</Button
              ></CollapsibleTrigger
            ><CollapsibleContent class="space-y-2 pt-3"
              ><div
                v-for="r in runtime.resources"
                :key="r.physical_id"
                class="rounded-md border p-3 space-y-1"
              >
                <p class="break-all font-medium">
                  {{ r.physical_id }} · {{ r.kind }} ·
                  {{ r.managed ? 'managed' : 'unknown / foreign' }} · {{ r.state }}
                </p>
                <p>
                  Allocated {{ storageBytes(r.allocated_bytes) }} · Logical
                  {{ storageBytes(r.logical_bytes) }} · Virtual
                  {{ storageBytes(r.virtual_bytes) }} · Shared {{ storageBytes(r.shared_bytes) }} ·
                  Reclaimable {{ storageBytes(r.reclaimable_bytes) }}
                </p>
                <p v-if="r.workspace">
                  {{ r.workspace.name }} · {{ r.workspace.owner_label }} · Intent
                  {{ r.workspace.status }} · Last activity
                  {{ r.workspace.last_activity_at ?? 'unknown' }}
                </p>
                <p class="break-all">
                  Aliases: {{ r.aliases.join(', ') || 'none' }} · Physical dependencies:
                  {{ r.dependencies.join(', ') || 'none' }}
                </p>
              </div></CollapsibleContent
            ></Collapsible
          >
        </CardContent></Card
      >
      <Button v-if="deleted.length" variant="outline" @click="showDeleted = !showDeleted">
        {{ showDeleted ? 'Hide deleted history' : 'Show deleted history' }} ({{ deleted.length }})
      </Button>
      <section v-for="group in groups" :key="group.name" class="space-y-3">
        <h3 class="font-semibold text-base">{{ group.name }} · {{ group.images.length }}</h3>
        <p v-if="!group.images.length">No stored generations.</p>
        <Card v-for="image in group.images" :key="image.id"
          ><CardHeader
            ><CardTitle class="flex flex-wrap gap-2 items-center"
              >{{ image.name
              }}<Badge>{{
                currentDefault(image)
                  ? 'Current default'
                  : image.status !== 'deleted' && image.is_pending
                    ? 'Pending attempt'
                    : image.origin_type === 'workspace_capture'
                      ? 'Capture'
                      : 'History'
              }}</Badge
              ><Badge variant="outline">{{ image.status }}</Badge></CardTitle
            ><CardDescription
              >{{ image.definition_name || image.owner_label }} · {{ image.runtime_type }} ·
              <template v-if="image.origin_type !== 'workspace_capture'">
                Generation {{ image.generation ?? 'legacy' }} · Revision
                {{ image.revision_id ?? 'unknown legacy recipe' }} </template
              ><template v-else>Capture</template></CardDescription
            ></CardHeader
          ><CardContent class="space-y-3">
            <p>
              Observed (last confirmed): {{ image.observed_state }} · Assignment availability:
              {{ image.assignment_status ?? 'not applicable' }} ·
              {{ storageBytes(image.size_bytes) }} ({{ image.size_source }})
            </p>
            <p class="break-all text-xs">
              {{ image.runner_ref || 'No physical reference confirmed' }} · {{ image.id }}
            </p>
            <Collapsible
              ><CollapsibleTrigger as-child
                ><Button variant="outline"
                  >Dependent workspaces ({{ image.dependencies.length }})</Button
                ></CollapsibleTrigger
              ><CollapsibleContent class="pt-3 space-y-2"
                ><p v-for="ws in image.dependencies" :key="ws.id">
                  {{ ws.name }} · {{ ws.owner_label }} · Observed {{ ws.observed_state }} / Intent
                  {{ ws.status }} · Last activity {{ ws.last_activity_at ?? 'unknown' }} ·
                  {{ ws.id }}
                </p></CollapsibleContent
              ></Collapsible
            >
            <div v-if="image.status !== 'deleted'" class="flex flex-wrap gap-2">
              <Button
                variant="outline"
                @click="target = { target_type: 'image', target_id: image.id }"
                >Delete this generation</Button
              ><Button
                v-if="image.build_job_id && currentDefault(image)"
                variant="outline"
                @click="target = { target_type: 'assignment', target_id: image.build_job_id! }"
                >Delete runner assignment (all generations)</Button
              ><Button
                v-if="
                  currentDefault(image) &&
                  image.definition_id &&
                  !['pending_deletion', 'deleting', 'deleted'].includes(image.status)
                "
                variant="outline"
                :disabled="
                  data.generations.some(
                    (i) =>
                      i.build_job_id === image.build_job_id &&
                      i.is_pending &&
                      ['pending', 'building', 'creating'].includes(i.status),
                  )
                "
                @click="rebuild(image.definition_id!)"
                >Build new generation</Button
              >
            </div>
          </CardContent></Card
        >
      </section>
      <section class="space-y-3">
        <h3 class="font-semibold">Deletion requests</h3>
        <Card v-for="request in deletions" :key="request.id"
          ><CardContent class="pt-4 space-y-2"
            ><p>{{ request.target_type }} · {{ request.mode }} · {{ request.phase }}</p>
            <p>
              {{
                request.phase === 'cancelled'
                  ? 'Cancelled — resources retained'
                  : request.phase === 'completed'
                    ? 'Completed'
                    : request.diagnostic || 'Waiting for server evidence'
              }}
            </p>
            <Button v-if="request.can_cancel" variant="outline" @click="cancel(request.id)"
              >Cancel pending request</Button
            ><Button
              v-if="request.phase === 'reconfirmation_required'"
              variant="outline"
              @click="target = { target_type: request.target_type, target_id: request.target_id }"
              >Review changed graph and confirm again</Button
            ></CardContent
          ></Card
        >
      </section>
      <section class="space-y-2">
        <h3 class="font-semibold">Capture pipeline</h3>
        <p v-for="capture in data.capture_requests" :key="capture.id">
          {{ capture.workspace_id }} → {{ capture.image_id }} · {{ capture.phase }} ·
          {{ capture.diagnostic }} {{ capture.resume_suppressed ? '(Restart suppressed)' : '' }}
        </p>
      </section>
      <RunnerOperationsPanel :operations="data.operations" @changed="load" />
    </template>
    <ImageDeletionDialog :target="target" @close="target = null" @requested="load" />
  </div>
</template>
