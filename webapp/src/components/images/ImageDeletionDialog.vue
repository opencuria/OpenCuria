<script setup lang="ts">
/**
 * Deletes an image, version, runner image or definition. By default the
 * deletion waits until no workspace uses it; admins can force a reviewed,
 * permanent deletion of exactly the previewed images and workspaces.
 */
import { computed, ref, watch } from 'vue'
import { Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogBody,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
import { useAuthStore } from '@/stores/auth'
import * as api from '@/services/runnerStorage.api'
import { sharedWorkspaceLabel } from '@opencuria/chat-core/imageVersions'
import type { DeletionTarget, DeletionGraph, DeletionRequest } from '@opencuria/client/types'
const props = defineProps<{ target: DeletionTarget | null; name?: string }>()
const emit = defineEmits<{ close: []; requested: [request: DeletionRequest] }>()
const isAdmin = () => useAuthStore().isAdmin
const KIND_LABELS: Record<DeletionTarget['target_type'], string> = {
  image: 'this version',
  captured_image: 'this image',
  assignment: 'this runner image',
  definition: 'this image definition',
}
/** What a deferred deletion does; it never deletes workspaces. */
const KIND_EFFECTS: Record<DeletionTarget['target_type'], string> = {
  image: 'It is deleted once no workspace uses it.',
  captured_image: 'All versions are deleted once no workspace uses them.',
  assignment: 'It is removed from the runner once no workspace uses it.',
  definition: 'Its images are deleted from all runners once no workspace uses them.',
}
const PHASE_LABELS: Record<string, string> = {
  intent: 'Deletion requested',
  waiting_inventory: 'Waiting for the runner',
  waiting_dependency: 'Waiting until unused',
  executing: 'Deleting…',
  reconfirmation_required: 'Needs your confirmation',
  attention_required: 'Needs attention',
  intervention: 'Needs attention',
  completed: 'Deleted',
  cancelled: 'Cancelled',
  failed: 'Failed',
}
/** Phases in which asking again does something; otherwise a request is underway. */
const REQUESTABLE_PHASES = new Set(['cancelled', 'failed', 'reconfirmation_required'])
const graph = ref<DeletionGraph | null>(null),
  result = ref<DeletionRequest | null>(null)
const error = ref(''),
  busy = ref(false),
  force = ref(false),
  approved = ref(false)
/** Runner images are removed from a runner; everything else is deleted. */
const verb = computed(() => (props.target?.target_type === 'assignment' ? 'Remove' : 'Delete'))
const subject = computed(() =>
  props.name ? props.name : props.target ? KIND_LABELS[props.target.target_type] : '',
)
const canRequest = computed(() => !result.value || REQUESTABLE_PHASES.has(result.value.phase))
const forceBlocked = computed(
  () => !approved.value || !graph.value || !!graph.value.blockers.length,
)
function phaseLabel(phase: string): string {
  return PHASE_LABELS[phase] ?? phase.replace(/_/g, ' ')
}
let version = 0
watch(
  () => props.target,
  async (target) => {
    const token = ++version
    graph.value = null
    result.value = null
    force.value = false
    approved.value = false
    error.value = ''
    busy.value = false
    if (!target) return
    busy.value = true
    try {
      const requests = await api.listDeletions()
      if (token !== version) return
      result.value =
        requests.find(
          (r) =>
            r.target_type === target.target_type &&
            r.target_id === target.target_id &&
            !['completed', 'cancelled'].includes(r.phase),
        ) ?? null
      if (isAdmin()) {
        const preview = await api.previewDeletion(target)
        if (token === version) graph.value = preview
      }
    } catch (e) {
      if (token === version) error.value = e instanceof Error ? e.message : 'Preview failed'
    } finally {
      if (token === version) busy.value = false
    }
  },
  { immediate: true },
)
async function request() {
  if (!props.target || (force.value && forceBlocked.value)) return
  busy.value = true
  error.value = ''
  try {
    result.value = await api.requestDeletion(
      props.target,
      force.value ? 'force' : 'deferred',
      force.value ? graph.value!.fingerprint : '',
    )
    approved.value = false
    emit('requested', result.value)
    if (result.value.phase === 'reconfirmation_required') await rePreview()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Deletion request failed'
    if (force.value) await rePreview()
  } finally {
    busy.value = false
  }
}
async function rePreview() {
  approved.value = false
  if (props.target) {
    try {
      graph.value = await api.previewDeletion(props.target)
    } catch (e) {
      graph.value = null
      error.value = e instanceof Error ? e.message : 'Preview failed'
    }
  }
}
async function cancel() {
  if (!result.value?.can_cancel) return
  busy.value = true
  try {
    result.value = await api.cancelDeletion(result.value.id)
    emit('requested', result.value)
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Cancellation failed'
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <Dialog :open="!!target" @update:open="(v) => !v && emit('close')">
    <DialogContent :class="force ? 'sm:max-w-2xl' : 'sm:max-w-lg'">
      <DialogHeader>
        <DialogTitle class="break-words">
          {{ force ? `Permanently delete ${subject}?` : `${verb} ${subject}?` }}
        </DialogTitle>
        <DialogDescription>
          {{
            force
              ? "Everything listed is deleted now, including other users' workspaces."
              : target
                ? `${KIND_EFFECTS[target.target_type]} Workspaces are kept.`
                : ''
          }}
        </DialogDescription>
      </DialogHeader>
      <DialogBody class="space-y-3">
        <p v-if="error" role="alert" class="text-sm text-destructive">{{ error }}</p>
        <p v-if="busy" role="status" class="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 class="size-4 animate-spin" aria-hidden="true" />
          Checking…
        </p>
        <div
          v-if="result"
          role="status"
          class="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border p-3"
        >
          <div class="min-w-0 space-y-0.5">
            <p class="text-sm font-medium">{{ phaseLabel(result.phase) }}</p>
            <p v-if="result.diagnostic" class="text-xs text-muted-foreground">
              {{ result.diagnostic }}
            </p>
          </div>
          <Button
            v-if="result.can_cancel"
            variant="outline"
            size="sm"
            :disabled="busy"
            @click="cancel"
            >Cancel deletion</Button
          >
        </div>
        <template v-if="force && graph">
          <p class="text-sm font-medium">
            {{ graph.counts.images }} images · {{ graph.counts.workspaces }} workspaces
          </p>
          <div
            class="max-h-64 space-y-1.5 overflow-auto rounded-md border border-border p-3 text-xs"
          >
            <p v-for="i in graph.images" :key="i.id">
              Image: {{ i.name }} · {{ i.owner_label || i.owner_id || 'unknown owner' }}
              <span class="text-muted-foreground">· {{ i.id }}</span>
            </p>
            <p v-for="w in graph.workspaces" :key="w.id">
              {{
                w.name
                  ? `Workspace: ${w.name} · ${w.owner_label || w.owner_id || 'unknown owner'}`
                  : sharedWorkspaceLabel(w)
              }}
              <span class="text-muted-foreground">· {{ w.id }}</span>
            </p>
            <p v-for="blocker in graph.blockers" :key="blocker" class="text-destructive">
              {{ blocker }}
            </p>
          </div>
          <label class="flex items-start gap-2 text-sm"
            ><Checkbox v-model="approved" :disabled="!!graph.blockers.length" />Delete exactly these
            items permanently</label
          >
        </template>
      </DialogBody>
      <DialogFooter class="flex-wrap">
        <Button
          v-if="target && isAdmin() && !force"
          variant="ghost"
          class="text-muted-foreground sm:mr-auto"
          :disabled="busy"
          @click="force = true"
          >Force delete…</Button
        >
        <Button
          v-if="force"
          variant="ghost"
          class="text-muted-foreground sm:mr-auto"
          :disabled="busy"
          @click="rePreview"
          >Refresh list</Button
        >
        <Button variant="outline" @click="emit('close')">{{
          canRequest || force ? 'Cancel' : 'Close'
        }}</Button>
        <Button
          v-if="force || canRequest"
          variant="destructive"
          :disabled="busy || (force && forceBlocked)"
          @click="request"
          >{{ force ? 'Delete permanently' : verb }}</Button
        >
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
