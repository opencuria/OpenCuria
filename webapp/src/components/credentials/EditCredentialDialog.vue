<script setup lang="ts">
import { ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
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
const name = ref('')
const value = ref('')
const submitting = ref(false)

function credentialDescriptor(credential: Credential): string {
  if (credential.credential_type === 'file') return credential.target_path
  if (credential.credential_type === 'ssh_key') return 'SSH key pair'
  return credential.env_var_name
}

watch(
  () => props.credential,
  (cred) => {
    if (cred) {
      open.value = true
      name.value = cred.name
      value.value = ''
    }
  },
)

async function handleSubmit(): Promise<void> {
  if (!props.credential) return

  submitting.value = true
  const data: { name?: string; value?: string } = {}
  if (name.value.trim() && name.value.trim() !== props.credential.name) {
    data.name = name.value.trim()
  }
  if (value.value) {
    data.value = value.value
  }

  if (data.name || data.value) {
    await credentialStore.updateCredential(props.credential.id, data)
  }
  submitting.value = false
  handleClose()
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    name.value = ''
    value.value = ''
    emit('close')
  }, 200)
}
</script>

<template>
  <Dialog
    :open="open"
    @update:open="(v) => (v ? null : handleClose())"
  >
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Edit Credential</DialogTitle>
        <DialogDescription>Update the credential name or replace its value.</DialogDescription>
      </DialogHeader>

      <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
        <div v-if="credential">
          <p class="text-sm text-muted-foreground mb-3">
            {{ credential.service_name }} — {{ credentialDescriptor(credential) }}
          </p>
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
          <Input v-model="name" placeholder="Credential name" />
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">New Value</label>
          <Textarea
            v-if="credential?.credential_type === 'file'"
            v-model="value"
            :rows="10"
            placeholder="Leave empty to keep current file contents"
          />
          <Input
            v-else
            v-model="value"
            type="password"
            placeholder="Leave empty to keep current value"
          />
          <p class="text-xs text-muted-foreground mt-1">
            Only fill this in if you want to replace the stored value.
          </p>
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
          <Button type="submit" :disabled="submitting">
            {{ submitting ? 'Saving…' : 'Save Changes' }}
          </Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
</template>
