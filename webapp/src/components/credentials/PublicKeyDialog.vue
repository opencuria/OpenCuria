<script setup lang="ts">
import { ref, watch } from 'vue'
import { UiDialog, UiButton } from '@/components/ui'
import { useCredentialStore } from '@/stores/credentials'
import type { Credential } from '@/types'
import { Copy, Check } from 'lucide-vue-next'

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
const copied = ref(false)

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

async function copyToClipboard(): Promise<void> {
  if (!publicKey.value) return
  try {
    await navigator.clipboard.writeText(publicKey.value)
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 2000)
  } catch {
    // Fallback
    const textarea = document.createElement('textarea')
    textarea.value = publicKey.value
    document.body.appendChild(textarea)
    textarea.select()
    document.execCommand('copy')
    document.body.removeChild(textarea)
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 2000)
  }
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    publicKey.value = ''
    emit('close')
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="SSH Public Key"
    description="Copy this public key and add it to your Git hosting provider or server."
    @update:open="(v) => (v ? null : handleClose())"
  >
    <div class="flex flex-col gap-4">
      <div v-if="loading" class="text-sm text-muted-fg">Loading public key...</div>

      <div v-else-if="publicKey">
        <div class="relative">
          <pre
            class="bg-muted rounded-[var(--radius-md)] p-3 text-xs text-fg font-mono break-all whitespace-pre-wrap select-all border border-border"
          >{{ publicKey }}</pre>
          <button
            class="absolute top-2 right-2 p-1.5 rounded-[var(--radius-sm)] bg-bg/80 hover:bg-surface-hover border border-border text-muted-fg hover:text-fg transition-colors cursor-pointer"
            title="Copy to clipboard"
            @click="copyToClipboard"
          >
            <Check v-if="copied" :size="14" class="text-success" />
            <Copy v-else :size="14" />
          </button>
        </div>
        <p class="text-xs text-muted-fg mt-2">
          Add this key to your Git provider (e.g. GitHub &rarr; Settings &rarr; SSH Keys)
          to enable SSH-based repository access in workspaces.
        </p>
      </div>

      <div v-else class="text-sm text-muted-fg">No public key available.</div>

      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" @click="handleClose">Close</UiButton>
        <UiButton v-if="publicKey" @click="copyToClipboard">
          {{ copied ? 'Copied!' : 'Copy Public Key' }}
        </UiButton>
      </div>
    </div>
  </UiDialog>
</template>
