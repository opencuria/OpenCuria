<script setup lang="ts">
/**
 * Dialog for capturing an image directly from a workspace card.
 * Used when the camera icon is clicked in WorkspaceActions.
 */
import { ref, computed } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { useWorkspaceStore } from '@/stores/workspaces'
import type { Workspace } from '@/types'

const props = defineProps<{
  workspace: Workspace
  open: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
}>()

const workspaceStore = useWorkspaceStore()

const approved = ref(false)
const error = ref('')
const name = ref('')
const submitting = ref(false)

const isValid = computed(
  () =>
    name.value.trim().length > 0 &&
    props.workspace.runtime_type === 'qemu' &&
    !props.workspace.intervention_required &&
    (props.workspace.status !== 'running' || approved.value),
)

async function handleSubmit(): Promise<void> {
  if (!isValid.value) return
  submitting.value = true
  const ok = await workspaceStore.createImageArtifact(props.workspace.id, {
    name: name.value.trim(),
    stop_and_restart: props.workspace.status === 'running' && approved.value,
  })
  submitting.value = false
  if (ok) handleClose()
  else
    error.value =
      'Capture refused. A stopped workspace requires controlled credential scrub proof; resume and stop it if externally stopped. Check notifications and runner operations for the exact diagnostic.'
}

function handleClose(): void {
  emit('update:open', false)
  setTimeout(() => {
    name.value = ''
    approved.value = false
    error.value = ''
  }, 200)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (!v ? handleClose() : undefined)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Capture Image</DialogTitle>
        <DialogDescription>
          Save the current state of this workspace as an image. Credentials must be off disk first —
          stop the workspace to strip them, then capture. If it was stopped externally, resume and
          stop it again.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="error" role="alert" class="text-destructive">{{ error }}</p>
        <label v-if="workspace.status === 'running'" class="flex gap-2 items-start mb-4"
          ><Checkbox v-model="approved" />I approve Stop → capture → restart. The workspace will be
          unavailable during capture. A failed restart preserves a valid image; inspect runner
          operations for diagnostics.</label
        >
        <form id="capture-image-form" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Image name</label>
            <Input v-model="name" placeholder="e.g. before-refactor" />
            <p class="text-xs text-muted-foreground mt-1">
              Workspace: <span class="font-mono">{{ workspace.name }}</span>
            </p>
          </div>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
        <Button type="submit" form="capture-image-form" :disabled="!isValid || submitting">
          {{ submitting ? 'Capturing…' : 'Capture Image' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
