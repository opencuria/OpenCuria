<script setup lang="ts">
/**
 * Dialog for cloning a workspace from an image.
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Check, Key } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import { useCredentialStore } from '@/stores/credentials'
import { toggleWorkspaceCredentialSelection } from '@/lib/workspaceCredentialSelection'
import type { ImageArtifact } from '@/types'

const props = defineProps<{
  imageArtifact: ImageArtifact
  disabled?: boolean
}>()

const router = useRouter()
const imageArtifactStore = useImageArtifactStore()
const credentialStore = useCredentialStore()

const open = ref(false)
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const submitting = ref(false)

const isValid = computed(() => name.value.trim().length > 0)

onMounted(async () => {
  await credentialStore.fetchCredentials()
})

function toggleCredential(id: string): void {
  const credential = credentialStore.credentials.find((entry) => entry.id === id)
  if (!credential) return
  selectedCredentialIds.value = toggleWorkspaceCredentialSelection(
    selectedCredentialIds.value,
    credential,
    credentialStore.credentials,
  )
}

async function handleSubmit(): Promise<void> {
  if (props.disabled) return
  if (!isValid.value) return
  submitting.value = true
  const workspaceId = await imageArtifactStore.createWorkspaceFromImageArtifact(
    props.imageArtifact.id,
    {
      name: name.value.trim(),
      credential_ids: selectedCredentialIds.value,
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
    selectedCredentialIds.value = []
  }, 200)
}
</script>

<template>
  <Dialog
    :open="open"
    @update:open="(v) => (v ? (!props.disabled && (open = true)) : handleClose())"
  >
    <DialogTrigger as-child>
      <slot />
    </DialogTrigger>

    <DialogContent>
      <DialogHeader>
        <DialogTitle>Clone Workspace from Image</DialogTitle>
        <DialogDescription>
          Creates a new workspace from this image. Choose credentials to inject for the initial setup.
        </DialogDescription>
      </DialogHeader>

      <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
        <div class="rounded-md bg-muted/50 border border-border p-3 text-sm">
          <div class="font-medium text-foreground mb-1">{{ props.imageArtifact.name }}</div>
          <div class="text-muted-foreground text-xs">
            Credentials are selected explicitly for the new workspace.
          </div>
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">New workspace name</label>
          <Input v-model="name" placeholder="e.g. feature-x" />
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Credentials <span class="text-muted-foreground font-normal">(optional)</span></label>
          <div v-if="credentialStore.credentials.length" class="flex flex-col gap-1.5 max-h-40 overflow-y-auto">
            <button
              v-for="cred in credentialStore.credentials"
              :key="cred.id"
              type="button"
              class="flex items-center gap-2 px-3 py-2 rounded-sm border text-left text-sm transition-colors cursor-pointer"
              :class="selectedCredentialIds.includes(cred.id)
                ? 'border-primary bg-primary/5 text-foreground'
                : 'border-border bg-background text-muted-foreground hover:bg-muted'"
              @click="toggleCredential(cred.id)"
            >
              <div
                class="flex items-center justify-center w-4 h-4 rounded-sm border"
                :class="selectedCredentialIds.includes(cred.id)
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border'"
              >
                <Check v-if="selectedCredentialIds.includes(cred.id)" :size="10" />
              </div>
              <span class="flex-1 truncate">{{ cred.name }}</span>
              <span v-if="cred.credential_type === 'ssh_key'" class="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <Key :size="10" />
                SSH Key
              </span>
              <span v-else-if="cred.target_path" class="text-xs text-muted-foreground">{{ cred.target_path }}</span>
              <span v-else-if="cred.env_var_name" class="text-xs text-muted-foreground">{{ cred.env_var_name }}</span>
            </button>
          </div>
          <p v-else class="text-xs text-muted-foreground">No credentials available.</p>
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
          <Button type="submit" :disabled="!isValid || submitting">
            {{ submitting ? 'Cloning…' : 'Clone Workspace' }}
          </Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
</template>
