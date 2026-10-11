<script setup lang="ts">
import { ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import CopyButton from '@/components/common/CopyButton.vue'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
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
const publicKey = ref('')
const loading = ref(false)

watch(
  () => props.credential,
  async (cred) => {
    if (cred && cred.has_public_key) {
      open.value = true
      loading.value = true
      const key = await credentialStore.getPublicKey(cred.id)
      publicKey.value = key ?? ''
      loading.value = false
    }
  },
)

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    publicKey.value = ''
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
        <DialogTitle>SSH Public Key</DialogTitle>
        <DialogDescription>
          Copy this public key and add it to your Git hosting provider or server.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
      <div class="flex flex-col gap-4">
        <div v-if="loading" class="text-sm text-muted-foreground">Loading public key...</div>

        <div v-else-if="publicKey">
          <div class="relative">
            <pre
              class="bg-muted rounded-md p-3 text-xs text-foreground font-mono break-all whitespace-pre-wrap select-all border border-border"
            >{{ publicKey }}</pre>
            <CopyButton
              :text="publicKey"
              label="public key"
              variant="outline"
              size="icon-sm"
              class="absolute top-2 right-2 bg-background/80 text-muted-foreground hover:text-foreground"
            />
          </div>
          <p class="text-xs text-muted-foreground mt-2">
            Add this key to your Git provider (e.g. GitHub &rarr; Settings &rarr; SSH Keys)
            to enable SSH-based repository access in workspaces.
          </p>
        </div>

        <div v-else class="text-sm text-muted-foreground">No public key available.</div>

      </div>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" @click="handleClose">Close</Button>
        <CopyButton v-if="publicKey" :text="publicKey" label="public key" variant="default" size="default">
          Copy Public Key
        </CopyButton>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
