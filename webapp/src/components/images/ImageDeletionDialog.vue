<script setup lang="ts">
import { ref, watch } from 'vue'
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
import type { DeletionTarget, DeletionGraph, DeletionRequest } from '@/types/runnerStorage'
const props = defineProps<{ target: DeletionTarget | null; name?: string }>()
const emit = defineEmits<{ close: []; requested: [request: DeletionRequest] }>()
const isAdmin = () => useAuthStore().isAdmin
const KIND_LABELS: Record<DeletionTarget['target_type'], string> = {
  image: 'image version',
  captured_image: 'image with all versions',
  assignment: 'runner build',
  definition: 'image definition',
}
const graph = ref<DeletionGraph | null>(null),
  result = ref<DeletionRequest | null>(null)
const error = ref(''),
  busy = ref(false),
  force = ref(false),
  approved = ref(false)
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
  if (
    !props.target ||
    (force.value && (!approved.value || !graph.value || graph.value.blockers.length))
  )
    return
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
    <DialogContent class="sm:max-w-2xl">
      <DialogHeader
        ><DialogTitle>{{
          force ? 'Force deletion — permanent data loss' : 'Request deletion'
        }}</DialogTitle>
        <DialogDescription
          >{{ name }} · {{ target ? KIND_LABELS[target.target_type] : '' }}. Ordinary deletion
          stores intent now and waits until no workspace uses it. It does not delete dependent
          workspaces.</DialogDescription
        ></DialogHeader
      >
      <DialogBody class="space-y-4">
        <p v-if="error" role="alert" class="text-destructive">{{ error }}</p>
        <p v-if="busy" role="status">Contacting server…</p>
        <div v-if="result" role="status" class="rounded-md border p-3">
          <p class="font-medium">{{ result.phase }}</p>
          <p>{{ result.diagnostic }}</p>
          <Button v-if="result.can_cancel" variant="outline" :disabled="busy" @click="cancel"
            >Cancel pending request</Button
          >
        </div>
        <template v-if="force">
          <p class="text-destructive font-medium">
            All listed images and workspaces will be permanently removed, including other owners'
            data.
          </p>
          <p v-if="graph">
            {{ graph.counts.images }} images · {{ graph.counts.workspaces }} workspaces
          </p>
          <div v-if="graph" class="max-h-64 overflow-auto space-y-2 rounded-md border p-3">
            <p v-for="i in graph.images" :key="i.id">
              Image: {{ i.name }} · Owner {{ i.owner_label || i.owner_id || 'unknown' }} ·
              {{ i.id }}
            </p>
            <p v-for="w in graph.workspaces" :key="w.id">
              Workspace: {{ w.name }} · Owner {{ w.owner_label || w.owner_id || 'unknown' }} ·
              {{ w.id }}
            </p>
            <p v-for="blocker in graph.blockers" :key="blocker" class="text-destructive">
              {{ blocker }}
            </p>
          </div>
          <label class="flex items-start gap-2"
            ><Checkbox v-model="approved" :disabled="!graph || !!graph.blockers.length" />I
            explicitly approve permanent deletion of exactly this preview.</label
          >
          <Button variant="outline" :disabled="busy" @click="rePreview">Revalidate preview</Button>
        </template>
      </DialogBody>
      <DialogFooter class="flex-wrap">
        <Button variant="outline" @click="emit('close')">Close</Button>
        <Button
          v-if="target && isAdmin() && !force"
          variant="outline"
          :disabled="busy"
          @click="force = true"
          >Review force deletion…</Button
        >
        <Button
          :variant="force ? 'destructive' : 'default'"
          :disabled="busy || (force && (!approved || !graph || !!graph.blockers.length))"
          @click="request"
          >{{ force ? 'Confirm permanent deletion' : 'Store deferred deletion intent' }}</Button
        >
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
