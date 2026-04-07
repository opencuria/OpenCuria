<script setup lang="ts">
/**
 * Dialog for capturing an image directly from a workspace card.
 * Used when the camera icon is clicked in WorkspaceActions.
 */
import { ref, computed } from 'vue'
import { UiDialog, UiInput, UiButton } from '@/components/ui'
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

const name = ref('')
const submitting = ref(false)

const isValid = computed(() => name.value.trim().length > 0)

async function handleSubmit(): Promise<void> {
  if (!isValid.value) return
  submitting.value = true
  await workspaceStore.createImageArtifact(props.workspace.id, { name: name.value.trim() })
  submitting.value = false
  handleClose()
}

function handleClose(): void {
  emit('update:open', false)
  setTimeout(() => {
    name.value = ''
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Capture Image"
    description="Save the current state of this workspace as an image. Attached credentials are stored in the image."
    @update:open="(v) => (!v ? handleClose() : undefined)"
  >
    <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Image name</label>
        <UiInput v-model="name" placeholder="e.g. before-refactor" />
        <p class="text-xs text-muted-fg mt-1">
          Workspace: <span class="font-mono">{{ workspace.name }}</span>
        </p>
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" @click="handleClose">Cancel</UiButton>
        <UiButton type="submit" :disabled="!isValid || submitting">
          {{ submitting ? 'Capturing…' : 'Capture Image' }}
        </UiButton>
      </div>
    </form>
  </UiDialog>
</template>
