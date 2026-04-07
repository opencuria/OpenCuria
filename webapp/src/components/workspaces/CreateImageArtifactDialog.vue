<script setup lang="ts">
/**
 * Dialog for capturing a new image from a workspace.
 * Shown on the global Images page.
 */
import { ref, computed, onMounted } from 'vue'
import { UiDialog, UiInput, UiButton, UiSelect } from '@/components/ui'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import { WorkspaceStatus, RuntimeType } from '@/types'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'

const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const imageArtifactStore = useImageArtifactStore()

const open = ref(false)
const name = ref('')
const selectedWorkspaceId = ref('')
const submitting = ref(false)

// Only QEMU workspaces that are running can be captured as images.
const snappableWorkspaces = computed(() =>
  workspaceStore.workspaces.filter(
    (w) => {
      const runner = runnerStore.runnerById(w.runner_id)
      return (
        w.runtime_type === RuntimeType.QEMU &&
        w.status === WorkspaceStatus.RUNNING &&
        runnerSupportsRuntime(runner, RuntimeType.QEMU)
      )
    },
  ),
)

const workspaceOptions = computed(() => [
  { value: '', label: '— Select a workspace —' },
  ...snappableWorkspaces.value.map((w) => ({
    value: w.id,
    label: w.name || w.id.slice(0, 8),
  })),
])

const isValid = computed(
  () => name.value.trim().length > 0 && selectedWorkspaceId.value !== '',
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
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Capture Image"
    description="Capture a point-in-time image of a running QEMU workspace. Attached credentials are saved in the image."
    @update:open="(v) => (v ? (open = true) : handleClose())"
  >
    <template #trigger>
      <UiButton @click="open = true">Capture Image</UiButton>
    </template>

    <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <!-- Image name -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Image name</label>
        <UiInput v-model="name" placeholder="e.g. before-refactor" />
      </div>

      <!-- Workspace selection -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Source workspace</label>
        <UiSelect v-model="selectedWorkspaceId" :options="workspaceOptions" />
        <p v-if="snappableWorkspaces.length === 0" class="text-xs text-muted-fg mt-1">
          No running QEMU workspaces found. Only running QEMU/KVM workspaces can be captured.
        </p>
        <p v-else class="text-xs text-muted-fg mt-1">
          Only running QEMU/KVM workspaces are shown.
        </p>
      </div>

      <!-- Actions -->
      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" @click="handleClose">Cancel</UiButton>
        <UiButton type="submit" :disabled="!isValid || submitting">
          {{ submitting ? 'Capturing…' : 'Capture Image' }}
        </UiButton>
      </div>
    </form>
  </UiDialog>
</template>
