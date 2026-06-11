<script setup lang="ts">
import { ref, watch } from 'vue'
import { useCredentialStore } from '@/stores/credentials'
import type { Credential } from '@/types'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

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
  <Dialog :open="open" @update:open="(v) => (v ? null : handleClose())">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Delete Credential</DialogTitle>
        <DialogDescription>This action cannot be undone.</DialogDescription>
      </DialogHeader>

      <div class="flex flex-col gap-4">
        <div v-if="credential" class="p-4 rounded-[var(--radius-md)] bg-destructive/10 border border-destructive/30">
          <p class="text-sm text-foreground">
            Are you sure you want to delete <strong>{{ credential.name }}</strong>?
          </p>
          <p class="text-xs text-muted-foreground mt-1">
            Workspaces that were created with this credential are not affected,
            but new workspaces will no longer be able to use it.
          </p>
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
          <Button variant="destructive" :disabled="deleting" @click="handleDelete">
            {{ deleting ? 'Deleting…' : 'Delete' }}
          </Button>
        </div>
      </div>
    </DialogContent>
  </Dialog>
</template>
