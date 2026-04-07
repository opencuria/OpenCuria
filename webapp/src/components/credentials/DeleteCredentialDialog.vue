<script setup lang="ts">
import { ref, watch } from 'vue'
import { UiDialog, UiButton } from '@/components/ui'
import { useCredentialStore } from '@/stores/credentials'
import type { Credential } from '@/types'

const props = defineProps<{
  credential: Credential | null
}>()

const emit = defineEmits<{
  close: []
}>()

const credentialStore = useCredentialStore()

const open = ref(false)
const deleting = ref(false)

watch(
  () => props.credential,
  (cred) => {
    if (cred) {
      open.value = true
    }
  },
)

async function handleDelete(): Promise<void> {
  if (!props.credential) return

  deleting.value = true
  await credentialStore.deleteCredential(props.credential.id)
  deleting.value = false
  handleClose()
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    emit('close')
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Delete Credential"
    description="This action cannot be undone."
    @update:open="(v) => (v ? null : handleClose())"
  >
    <div class="flex flex-col gap-4">
      <div v-if="credential" class="p-4 rounded-[var(--radius-md)] bg-error-muted border border-error/30">
        <p class="text-sm text-fg">
          Are you sure you want to delete <strong>{{ credential.name }}</strong>?
        </p>
        <p class="text-xs text-muted-fg mt-1">
          Workspaces that were created with this credential are not affected,
          but new workspaces will no longer be able to use it.
        </p>
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" @click="handleClose">Cancel</UiButton>
        <UiButton variant="destructive" :disabled="deleting" @click="handleDelete">
          {{ deleting ? 'Deleting…' : 'Delete' }}
        </UiButton>
      </div>
    </div>
  </UiDialog>
</template>
