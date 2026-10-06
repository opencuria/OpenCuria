<script setup lang="ts">
/**
 * Reset a workspace onto its own image version or update it to the latest
 * version. The workspace keeps its id, chats and settings; all data stored
 * only inside the workspace is permanently deleted.
 */
import { computed, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { recreateBlocker } from '@/lib/imageVersions'
import { useWorkspaceStore } from '@/stores/workspaces'
import type { Workspace } from '@/types'

const props = defineProps<{
  workspace: Workspace
  open: boolean
  preferLatest?: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
}>()

const workspaceStore = useWorkspaceStore()

/** A failed reset keeps its target pending; retrying it is the "current" reset. */
const base = computed(() => props.workspace.pending_base_image ?? props.workspace.base_image)

const options = computed(() => {
  const current = base.value
  if (!current) return []
  const result: { id: string; kind: 'reset' | 'update'; label: string }[] = []
  if (current.status === 'ready') {
    result.push({
      id: current.id,
      kind: 'reset',
      label: `Reset to v${current.version ?? '?'} (current)`,
    })
  }
  if (current.update_available && current.latest_id) {
    result.push({
      id: current.latest_id,
      kind: 'update',
      label: `Update to v${current.latest_version ?? '?'} (latest)`,
    })
  }
  return result
})

const selectedId = ref('')
const confirmed = ref(false)
const submitting = ref(false)

const selected = computed(() => options.value.find((option) => option.id === selectedId.value))
const blocker = computed(() => (options.value.length ? null : recreateBlocker(base.value)))

watch(
  () => props.open,
  (open) => {
    if (!open) return
    const latest = options.value.find((option) => option.kind === 'update')
    const current = options.value.find((option) => option.kind === 'reset')
    selectedId.value = (props.preferLatest ? latest?.id : current?.id) ?? options.value[0]?.id ?? ''
    confirmed.value = false
  },
  { immediate: true },
)

async function handleSubmit(): Promise<void> {
  if (!selected.value || !confirmed.value) return
  submitting.value = true
  const ok = await workspaceStore.recreateWorkspace(props.workspace.id, selected.value.id)
  submitting.value = false
  if (ok) emit('update:open', false)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Reset workspace</DialogTitle>
        <DialogDescription>
          Recreates <span class="font-medium">{{ workspace.name }}</span> from
          <span class="font-medium">{{ base?.name ?? 'its image' }}</span
          >.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="blocker" role="alert" class="text-sm text-destructive">{{ blocker }}</p>
        <form v-else id="recreate-workspace-form" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
          <Select v-if="options.length > 1" v-model="selectedId" :disabled="submitting">
            <SelectTrigger data-testid="recreate-target-select">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem v-for="option in options" :key="option.id" :value="option.id">
                {{ option.label }}
              </SelectItem>
            </SelectContent>
          </Select>
          <p v-else class="text-sm text-foreground">{{ selected?.label }}</p>

          <div
            class="rounded-[var(--radius-md)] border border-destructive/40 bg-destructive/5 p-3 text-sm"
            data-testid="recreate-data-loss-warning"
          >
            <p class="font-medium text-destructive">Workspace data is permanently deleted</p>
            <p class="mt-1 text-muted-foreground">
              Everything stored only in this workspace is lost: files, installed packages,
              uncommitted changes and running processes. Chats, settings, credentials and schedules
              are kept.
            </p>
            <p v-if="workspace.repos?.length" class="mt-1 text-muted-foreground">
              Cloned again: <span class="font-mono">{{ workspace.repos.join(', ') }}</span>
            </p>
          </div>

          <label class="flex items-center gap-2 text-sm">
            <Checkbox v-model="confirmed" data-testid="recreate-confirm" :disabled="submitting" />
            I understand this cannot be undone
          </label>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" @click="emit('update:open', false)">Cancel</Button>
        <Button
          v-if="!blocker"
          type="submit"
          form="recreate-workspace-form"
          variant="destructive"
          data-testid="recreate-submit"
          :disabled="!selected || !confirmed || submitting"
        >
          {{ selected?.kind === 'update' ? 'Update workspace' : 'Reset workspace' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
