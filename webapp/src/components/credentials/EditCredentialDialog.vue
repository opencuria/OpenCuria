<script setup lang="ts">
import { ref, watch } from 'vue'
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
const error = ref<string | null>(null)

function credentialDescriptor(credential: Credential): string {
  if (credential.credential_type === 'mcp_oauth') return 'OAuth'
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
      error.value = null
    }
  },
  { immediate: true },
)

async function handleSubmit(): Promise<void> {
  const credential = props.credential
  if (!credential || submitting.value || !name.value.trim()) return
  const data: { name?: string; value?: string } = {}
  if (name.value.trim() !== credential.name) data.name = name.value.trim()
  if (value.value) data.value = value.value
  if (!Object.keys(data).length) {
    handleClose()
    return
  }
  submitting.value = true
  error.value = null
  let saved = false
  try {
    saved = await credentialStore.updateCredential(credential.id, data)
    if (!saved) error.value = 'Failed to update credential.'
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to update credential.'
  } finally {
    submitting.value = false
  }
  if (saved) handleClose()
}

function handleClose(): void {
  if (submitting.value) return
  open.value = false
  name.value = ''
  value.value = ''
  error.value = null
  emit('close')
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (v ? null : handleClose())">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Edit Credential</DialogTitle>
        <DialogDescription>{{
          credential?.credential_type === 'mcp_oauth'
            ? 'Update the credential name.'
            : 'Update the credential name or replace its value.'
        }}</DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="error" class="text-sm text-destructive" role="alert">{{ error }}</p>
        <form id="edit-credential-form" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
          <div v-if="credential">
            <p class="text-sm text-muted-foreground mb-3">
              {{ credential.service_name }} — {{ credentialDescriptor(credential) }}
            </p>
          </div>

          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
            <Input v-model="name" :disabled="submitting" placeholder="Credential name" />
          </div>

          <div v-if="credential?.credential_type !== 'mcp_oauth'">
            <label class="text-sm font-medium text-foreground mb-1.5 block">New Value</label>
            <Textarea
              v-if="credential?.credential_type === 'file'"
              v-model="value"
              :disabled="submitting"
              :rows="6"
              placeholder="Leave empty to keep current file contents"
            />
            <Input
              v-else
              v-model="value"
              :disabled="submitting"
              type="password"
              placeholder="Leave empty to keep current value"
            />
            <p class="text-xs text-muted-foreground mt-1">
              Only fill this in if you want to replace the stored value.
            </p>
          </div>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="submitting" @click="handleClose">Cancel</Button>
        <Button type="submit" form="edit-credential-form" :disabled="submitting || !name.trim()">
          {{ submitting ? 'Saving…' : 'Save Changes' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
