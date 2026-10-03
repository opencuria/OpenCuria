<script setup lang="ts">
/**
 * Dialog for capturing a new image from a workspace.
 * Shown on the global Images page.
 */
import { ref, computed, onMounted, watch } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import { WorkspaceStatus, RuntimeType } from '@/types'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'

const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const imageArtifactStore = useImageArtifactStore()

const open = ref(false)
const approved = ref(false)
const name = ref('')
const selectedWorkspaceId = ref('')
const submitting = ref(false)

const snappableWorkspaces = computed(() =>
  workspaceStore.workspaces.filter((w) => {
    const runner = runnerStore.runnerById(w.runner_id)
    return (
      w.runtime_type === RuntimeType.QEMU &&
      !w.intervention_required &&
      (w.status === WorkspaceStatus.RUNNING || !w.credentials_present) &&
      (w.status === WorkspaceStatus.RUNNING || w.status === WorkspaceStatus.STOPPED) &&
      runnerSupportsRuntime(runner, RuntimeType.QEMU)
    )
  }),
)

const blockedByCredentials = computed(() =>
  workspaceStore.workspaces.some(
    (w) =>
      w.runtime_type === RuntimeType.QEMU &&
      w.credentials_present &&
      (w.status === WorkspaceStatus.RUNNING || w.status === WorkspaceStatus.STOPPED),
  ),
)

const workspaceOptions = computed(() => [
  { value: '', label: '— Select a workspace —' },
  ...snappableWorkspaces.value.map((w) => ({
    value: w.id,
    label: w.name || w.id.slice(0, 8),
  })),
])

const selected = computed(() =>
  snappableWorkspaces.value.find((w) => w.id === selectedWorkspaceId.value),
)
watch(selectedWorkspaceId, () => {
  approved.value = false
})
const isValid = computed(
  () =>
    name.value.trim().length > 0 &&
    !!selected.value &&
    (selected.value.status !== WorkspaceStatus.RUNNING || approved.value),
)

onMounted(async () => {
  if (!workspaceStore.workspaces.length) {
    await workspaceStore.fetchWorkspaces()
  }
  if (!runnerStore.runners.length) {
    await runnerStore.fetchRunners()
  }
})

async function handleSubmit(): Promise<void> {
  if (!isValid.value) return
  submitting.value = true
  const ok = await imageArtifactStore.createImageArtifact({
    name: name.value.trim(),
    workspace_id: selectedWorkspaceId.value,
    stop_and_restart: selected.value?.status === WorkspaceStatus.RUNNING && approved.value,
  })
  submitting.value = false
  if (ok) {
    handleClose()
  }
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    name.value = ''
    selectedWorkspaceId.value = ''
    approved.value = false
  }, 200)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (v ? (open = true) : handleClose())">
    <DialogTrigger as-child>
      <Button size="sm" @click="open = true">Capture Image</Button>
    </DialogTrigger>

    <DialogContent>
      <DialogHeader>
        <DialogTitle>Capture Image</DialogTitle>
        <DialogDescription>
          Capture a point-in-time image of a QEMU workspace. Credentials must be removed first —
          stop the workspace to strip them, then capture. If it was stopped externally, resume and
          stop it again.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="imageArtifactStore.error" role="alert" class="text-destructive">
          {{ imageArtifactStore.error }}
        </p>
        <label
          v-if="selected?.status === WorkspaceStatus.RUNNING"
          class="flex gap-2 items-start mb-4"
          ><Checkbox v-model="approved" />I approve Stop → capture → restart. This temporarily
          interrupts the workspace. If restart fails the image is preserved; inspect runner
          operations.</label
        >
        <form
          id="create-image-artifact-form"
          class="flex flex-col gap-4"
          @submit.prevent="handleSubmit"
        >
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Image name</label>
            <Input v-model="name" placeholder="e.g. before-refactor" />
          </div>

          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Source workspace</label>
            <Select v-model="selectedWorkspaceId">
              <SelectTrigger>
                <SelectValue placeholder="Select a workspace" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem
                  v-for="opt in workspaceOptions"
                  :key="opt.value || 'empty'"
                  :value="opt.value"
                  :disabled="opt.value === ''"
                >
                  {{ opt.label }}
                </SelectItem>
              </SelectContent>
            </Select>
            <p v-if="snappableWorkspaces.length === 0" class="text-xs text-muted-foreground mt-1">
              No capturable QEMU workspaces found. Capture requires a QEMU workspace with no
              intervention fence. Running workspaces require explicit stop/capture/restart approval.
            </p>
            <p v-else class="text-xs text-muted-foreground mt-1">
              Stopped workspaces require controlled credential scrub proof. Running workspaces
              require explicit approval.
            </p>
            <p v-if="blockedByCredentials" class="text-xs text-muted-foreground mt-1">
              Stopped workspace credentials require controlled resume/stop scrub proof. Unknown
              proof is rejected by the runner, never assumed clean.
            </p>
          </div>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
        <Button type="submit" form="create-image-artifact-form" :disabled="!isValid || submitting">
          {{ submitting ? 'Capturing…' : 'Capture Image' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
