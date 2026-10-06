<script setup lang="ts">
/**
 * Dialog for capturing a new image from a workspace.
 * Shown on the global Images page.
 */
import { ref, computed, onMounted } from 'vue'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import CaptureTargetFields from '@/components/images/CaptureTargetFields.vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import { WorkspaceStatus, RuntimeType } from '@/types'
import { NEW_IMAGE } from '@/lib/imageVersions'
import { runnerSupportsRuntime } from '@/lib/runtimeSupport'

const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const imageArtifactStore = useImageArtifactStore()

const open = ref(false)
const target = ref(NEW_IMAGE)
const name = ref('')
const message = ref('')
const selectedWorkspaceId = ref('')
const submitting = ref(false)
const fields = ref<InstanceType<typeof CaptureTargetFields> | null>(null)

const snappableWorkspaces = computed(() =>
  workspaceStore.workspaces.filter((w) => {
    const runner = runnerStore.runnerById(w.runner_id)
    return (
      w.runtime_type === RuntimeType.QEMU &&
      !w.intervention_required &&
      !workspaceStore.isWorkspaceTransitioning(w.id) &&
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
  ...snappableWorkspaces.value.map((w) => ({
    value: w.id,
    label: w.name || w.id.slice(0, 8),
  })),
])

const captureBlocked = computed(
  () =>
    submitting.value ||
    (!!selectedWorkspaceId.value &&
      workspaceStore.isWorkspaceTransitioning(selectedWorkspaceId.value)),
)
const selected = computed(() =>
  snappableWorkspaces.value.find((w) => w.id === selectedWorkspaceId.value),
)
const isValid = computed(
  () =>
    !!fields.value?.isValid &&
    !!selected.value &&
    !workspaceStore.isWorkspaceTransitioning(selected.value.id),
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
  if (!isValid.value || !fields.value) return
  submitting.value = true
  const ok = await imageArtifactStore.createImageArtifact({
    ...fields.value.payload(),
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
    message.value = ''
    selectedWorkspaceId.value = ''
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
          The workspace briefly stops and continues on the captured version. Files, chats and
          settings are kept; running processes stop.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="imageArtifactStore.error" role="alert" class="text-destructive">
          {{ imageArtifactStore.error }}
        </p>
        <form
          id="create-image-artifact-form"
          class="flex flex-col gap-4"
          @submit.prevent="handleSubmit"
        >
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Source workspace</label>
            <Select v-model="selectedWorkspaceId" :disabled="captureBlocked">
              <SelectTrigger>
                <SelectValue placeholder="Select a workspace" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem v-for="opt in workspaceOptions" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </SelectItem>
              </SelectContent>
            </Select>
            <p v-if="snappableWorkspaces.length === 0" class="text-xs text-muted-foreground mt-1">
              No capturable QEMU workspaces found. Capture requires a QEMU workspace with no
              intervention fence.
            </p>
            <p v-else class="text-xs text-muted-foreground mt-1">
              Stopped workspaces require controlled credential scrub proof.
            </p>
            <p v-if="blockedByCredentials" class="text-xs text-muted-foreground mt-1">
              Stopped workspace credentials require controlled resume/stop scrub proof. Unknown
              proof is rejected by the runner, never assumed clean.
            </p>
          </div>
          <CaptureTargetFields
            ref="fields"
            v-model:target="target"
            v-model:name="name"
            v-model:message="message"
            :workspace="selected ?? null"
            :disabled="captureBlocked"
          />
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
        <Button type="submit" form="create-image-artifact-form" :disabled="!isValid || submitting">
          {{ submitting ? 'Capturing' : 'Capture Image' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
