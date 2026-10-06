<script setup lang="ts">
import { computed } from 'vue'
import { ArrowUpRight, ChevronDown, MoreHorizontal, RotateCw, Trash2, X } from '@lucide/vue'
import type { DeletionTarget, StorageGeneration } from '@/types/runnerStorage'
import type { RunnerTopologyNode } from '@/lib/runnerTopology'
import {
  currentRunnerDefault,
  generationLabel,
  runnerStateClass,
  runnerStateLabel,
} from '@/lib/runnerPresentation'
import { storageBytes } from '@/composables/useRunnerStorage'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
const props = defineProps<{
  node: RunnerTopologyNode
  generations: StorageGeneration[]
  busy?: boolean
}>()
const emit = defineEmits<{
  close: []
  rebuild: [definitionId: string]
  delete: [target: DeletionTarget, name: string]
}>()
const image = computed(() => props.node.generation)
const resource = computed(() => props.node.resource)
const workspace = computed(() => props.node.workspace)
const canRebuild = computed(
  () =>
    image.value &&
    currentRunnerDefault(image.value) &&
    image.value.definition_id &&
    !['pending_deletion', 'deleting', 'deleted'].includes(image.value.status),
)
const buildPending = computed(
  () =>
    !!props.busy ||
    props.generations.some(
      (i) =>
        i.build_job_id === image.value?.build_job_id &&
        i.is_pending &&
        ['pending', 'building', 'creating'].includes(i.status),
    ),
)
function openWorkspace() {
  window.dispatchEvent(new CustomEvent('opencuria:close-settings'))
}
</script>
<template>
  <section
    aria-label="Selected resource"
    class="min-w-0 overflow-hidden rounded-xl border border-primary/25 bg-card"
    data-testid="runner-inspector"
  >
    <div class="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
      <div class="min-w-0 space-y-1">
        <h4 class="truncate text-sm font-semibold">{{ node.label }}</h4>
        <div class="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          <span
            >{{ node.runtime?.toUpperCase() }} ·
            {{ resource?.kind || (workspace ? 'workspace' : 'image') }}</span
          >
          <Badge v-if="image" variant="secondary">{{ generationLabel(image) }}</Badge>
          <Badge
            variant="outline"
            :class="
              runnerStateClass(
                resource?.state || image?.observed_state || workspace?.observed_state,
              )
            "
            >Observed:
            {{
              runnerStateLabel(
                resource?.state || image?.observed_state || workspace?.observed_state,
              )
            }}</Badge
          >
          <Badge v-if="image || workspace" variant="outline"
            >Lifecycle: {{ runnerStateLabel(image?.status || workspace?.status) }}</Badge
          >
        </div>
      </div>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Close resource details"
        @click="emit('close')"
        ><X
      /></Button>
    </div>
    <div class="space-y-4 p-4 text-xs">
      <dl class="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
        <div v-if="image">
          <dt class="text-muted-foreground">Image size</dt>
          <dd class="mt-1 font-medium tabular-nums">{{ storageBytes(image.size_bytes) }}</dd>
          <dd class="text-[11px] text-muted-foreground">{{ image.size_source }}</dd>
        </div>
        <template v-if="node.storage">
          <div>
            <dt class="text-muted-foreground">Used storage</dt>
            <dd class="mt-1 font-medium tabular-nums">
              {{ storageBytes(node.storage.allocatedBytes) }}
            </dd>
            <dd v-if="node.storage.unknownCount" class="text-[11px] text-muted-foreground">
              Partial known total · {{ node.storage.unknownCount }} resource(s) unknown
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Disk capacity</dt>
            <dd class="mt-1 font-medium tabular-nums">
              {{ storageBytes(node.storage.virtualBytes) }}
            </dd>
          </div>
          <div>
            <dt class="text-muted-foreground">File size</dt>
            <dd class="mt-1 font-medium tabular-nums">
              {{ storageBytes(node.storage.logicalBytes) }}
            </dd>
          </div>
        </template>
        <template v-else-if="resource && !['workspace', 'container'].includes(resource.kind)">
          <div
            v-for="(value, label) in {
              Allocated: resource.allocated_bytes,
              Logical: resource.logical_bytes,
              Virtual: resource.virtual_bytes,
              Shared: resource.shared_bytes,
              Reclaimable: resource.reclaimable_bytes,
            }"
            :key="label"
          >
            <dt class="text-muted-foreground">{{ label }}</dt>
            <dd class="mt-1 font-medium tabular-nums">{{ storageBytes(value) }}</dd>
          </div>
        </template>
        <div v-if="workspace || image">
          <dt class="text-muted-foreground">Owner</dt>
          <dd class="mt-1 font-medium">
            {{ workspace?.owner_label || image?.owner_label || 'Unknown' }}
          </dd>
        </div>
        <div v-if="workspace">
          <dt class="text-muted-foreground">Last activity</dt>
          <dd class="mt-1 break-words">{{ workspace.last_activity_at || 'Unknown' }}</dd>
        </div>
        <template v-if="image && image.origin_type !== 'workspace_capture'">
          <div>
            <dt class="text-muted-foreground">Definition</dt>
            <dd class="mt-1">{{ image.definition_name || 'Unknown' }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Version</dt>
            <dd class="mt-1">{{ image.generation ?? 'Legacy' }}</dd>
          </div>
          <div>
            <dt class="text-muted-foreground">Assignment</dt>
            <dd class="mt-1">{{ image.assignment_status || 'Not applicable' }}</dd>
          </div>
        </template>
      </dl>
      <p v-if="resource && !resource.managed" class="text-warning">
        Unmanaged resource · ownership is not confirmed
      </p>
      <p v-if="node.unresolved" class="text-warning">
        Dependency target is not present in the confirmed inventory.
      </p>
      <div v-if="image?.dependencies.length" class="space-y-2">
        <p class="font-medium">Dependent workspaces · {{ image.dependencies.length }}</p>
        <div class="divide-y divide-border rounded-lg border border-border">
          <div
            v-for="ws in image.dependencies"
            :key="ws.id"
            class="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
          >
            <div class="min-w-0">
              <p class="font-medium">{{ ws.name }}</p>
              <p class="text-muted-foreground">
                {{ ws.owner_label }} · {{ ws.last_activity_at || 'Activity unknown' }}
                <template v-if="ws.allocated_bytes != null">
                  · {{ storageBytes(ws.allocated_bytes) }} own disk</template
                >
                <template v-if="ws.pending_base_image_instance_id === image.id">
                  · switching to this version</template
                >
              </p>
            </div>
            <div class="flex flex-wrap gap-1">
              <Badge variant="outline">Observed: {{ ws.observed_state || 'unknown' }}</Badge
              ><Badge variant="secondary">Lifecycle: {{ ws.status }}</Badge>
            </div>
          </div>
        </div>
      </div>
      <Collapsible>
        <CollapsibleTrigger as-child
          ><Button variant="ghost" size="sm" class="px-0 text-xs text-muted-foreground"
            >Technical details<ChevronDown /></Button
        ></CollapsibleTrigger>
        <CollapsibleContent class="space-y-2 pt-2 text-muted-foreground [overflow-wrap:anywhere]">
          <p v-if="resource">
            Physical ID: <code>{{ resource.physical_id }}</code>
          </p>
          <p v-if="resource">Aliases: {{ resource.aliases.join(', ') || 'None' }}</p>
          <p v-if="resource">
            Physical dependencies: {{ resource.dependencies.join(', ') || 'None' }}
          </p>
          <p v-if="resource">Provenance: {{ resource.provenance || 'Unknown' }}</p>
          <Collapsible v-if="node.storage?.resources.length">
            <CollapsibleTrigger as-child>
              <Button variant="ghost" size="sm" class="px-0 text-xs"
                >Storage resources ({{ node.storage.resources.length }})<ChevronDown
              /></Button>
            </CollapsibleTrigger>
            <CollapsibleContent class="space-y-3 pt-2">
              <div
                v-for="member in node.storage.resources"
                :key="member.physical_id"
                class="space-y-1 rounded-lg border p-3"
              >
                <p>
                  Physical ID: <code>{{ member.physical_id }}</code>
                </p>
                <p>
                  {{ member.kind }} · Observed: {{ runnerStateLabel(member.state) }} ·
                  {{ member.managed ? 'Managed' : 'Unmanaged' }}
                </p>
                <dl class="grid grid-cols-2 gap-2">
                  <div
                    v-for="(value, label) in {
                      Allocated: member.allocated_bytes,
                      Logical: member.logical_bytes,
                      Virtual: member.virtual_bytes,
                      Shared: member.shared_bytes,
                      Reclaimable: member.reclaimable_bytes,
                    }"
                    :key="label"
                  >
                    <dt>{{ label }}</dt>
                    <dd>{{ storageBytes(value) }}</dd>
                  </div>
                </dl>
                <p>Aliases: {{ member.aliases.join(', ') || 'None' }}</p>
                <p>Physical dependencies: {{ member.dependencies.join(', ') || 'None' }}</p>
                <p>Provenance: {{ member.provenance || 'Unknown' }}</p>
              </div>
            </CollapsibleContent>
          </Collapsible>
          <p v-if="image">
            Image ID: <code>{{ image.id }}</code>
          </p>
          <p v-if="image">Physical reference: {{ image.runner_ref || 'Unconfirmed' }}</p>
          <p v-if="image && image.origin_type !== 'workspace_capture'">
            Revision: {{ image.revision_id || 'Unknown legacy recipe' }}
          </p>
          <p v-if="workspace">
            Workspace ID: <code>{{ workspace.id }}</code>
          </p>
        </CollapsibleContent>
      </Collapsible>
      <div class="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <Button v-if="workspace" as-child variant="outline" size="sm"
          ><RouterLink :to="`/workspaces/${workspace.id}`" @click="openWorkspace"
            >Open workspace<ArrowUpRight /></RouterLink
        ></Button>
        <Button
          v-if="canRebuild"
          variant="outline"
          size="sm"
          :disabled="buildPending"
          @click="emit('rebuild', image!.definition_id!)"
          ><RotateCw />Build new generation</Button
        >
        <DropdownMenu v-if="image && image.status !== 'deleted'">
          <DropdownMenuTrigger as-child
            ><Button variant="outline" size="sm" :disabled="busy" aria-label="Image actions"
              ><MoreHorizontal />Actions</Button
            ></DropdownMenuTrigger
          >
          <DropdownMenuContent align="end">
            <DropdownMenuItem
              class="text-destructive"
              @select="emit('delete', { target_type: 'image', target_id: image!.id }, image!.name)"
              ><Trash2 />Delete this generation</DropdownMenuItem
            >
            <template v-if="image.build_job_id && currentRunnerDefault(image)"
              ><DropdownMenuSeparator /><DropdownMenuItem
                class="text-destructive"
                @select="
                  emit(
                    'delete',
                    { target_type: 'assignment', target_id: image!.build_job_id! },
                    image!.definition_name || image!.name,
                  )
                "
                >Delete runner assignment (all generations)</DropdownMenuItem
              ></template
            >
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  </section>
</template>
