<script setup lang="ts">
/**
 * Dialog for cloning a workspace from an image.
 * The image already contains all credentials; the user only needs to
 * provide a name for the new workspace.
 */
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { UiDialog, UiInput, UiButton } from '@/components/ui'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import type { ImageArtifact } from '@/types'

const props = defineProps<{
  imageArtifact: ImageArtifact
  disabled?: boolean
}>()

const router = useRouter()
const imageArtifactStore = useImageArtifactStore()

const open = ref(false)
const name = ref('')
const submitting = ref(false)

const isValid = computed(() => name.value.trim().length > 0)

async function handleSubmit(): Promise<void> {
  if (props.disabled) return
  if (!isValid.value) return
  submitting.value = true
  const workspaceId = await imageArtifactStore.createWorkspaceFromImageArtifact(
    props.imageArtifact.id,
    {
      name: name.value.trim(),
    },
  )
  submitting.value = false
  if (workspaceId) {
    handleClose()
    router.push(`/workspaces/${workspaceId}`)
  }
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    name.value = ''
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Clone Workspace from Image"
    description="Creates a new QEMU workspace from this image. Credentials from the image are automatically restored — no need to re-configure them."
    @update:open="(v) => (v ? (!props.disabled && (open = true)) : handleClose())"
  >
    <template #trigger>
      <slot />
    </template>

    <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <!-- Image info -->
      <div class="rounded-[var(--radius-md)] bg-bg-subtle border border-border p-3 text-sm">
        <div class="font-medium text-fg mb-1">{{ props.imageArtifact.name }}</div>
        <div class="text-muted-fg text-xs">
          {{ props.imageArtifact.credential_ids.length }} credential(s) will be restored automatically.
        </div>
      </div>

      <!-- New workspace name -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">New workspace name</label>
        <UiInput v-model="name" placeholder="e.g. feature-x" />
      </div>

      <!-- Actions -->
      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" @click="handleClose">Cancel</UiButton>
        <UiButton type="submit" :disabled="!isValid || submitting">
          {{ submitting ? 'Cloning…' : 'Clone Workspace' }}
        </UiButton>
      </div>
    </form>
  </UiDialog>
</template>
