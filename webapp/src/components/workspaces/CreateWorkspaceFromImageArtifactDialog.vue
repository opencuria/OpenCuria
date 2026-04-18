<script setup lang="ts">
/**
 * Dialog for cloning a workspace from an image.
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { UiDialog, UiInput, UiButton } from '@/components/ui'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import { useCredentialStore } from '@/stores/credentials'
import { Check, Key } from 'lucide-vue-next'
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
  const idx = selectedCredentialIds.value.indexOf(id)
  if (idx === -1) {
    selectedCredentialIds.value.push(id)
  } else {
    selectedCredentialIds.value.splice(idx, 1)
  }
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
  <UiDialog
    :open="open"
    title="Clone Workspace from Image"
    description="Creates a new workspace from this image. Choose credentials to inject for the initial setup."
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
          Credentials are selected explicitly for the new workspace.
        </div>
      </div>

      <!-- New workspace name -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">New workspace name</label>
        <UiInput v-model="name" placeholder="e.g. feature-x" />
      </div>

      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Credentials <span class="text-muted-fg font-normal">(optional)</span></label>
        <div v-if="credentialStore.credentials.length" class="flex flex-col gap-1.5 max-h-40 overflow-y-auto">
          <button
            v-for="cred in credentialStore.credentials"
            :key="cred.id"
            type="button"
            class="flex items-center gap-2 px-3 py-2 rounded-[var(--radius-sm)] border text-left text-sm transition-colors cursor-pointer"
            :class="selectedCredentialIds.includes(cred.id)
              ? 'border-primary bg-primary/5 text-fg'
              : 'border-border bg-bg text-muted-fg hover:bg-surface-hover'"
            @click="toggleCredential(cred.id)"
          >
            <div
              class="flex items-center justify-center w-4 h-4 rounded-sm border"
              :class="selectedCredentialIds.includes(cred.id)
                ? 'border-primary bg-primary text-primary-fg'
                : 'border-border'"
            >
              <Check v-if="selectedCredentialIds.includes(cred.id)" :size="10" />
            </div>
            <span class="flex-1 truncate">{{ cred.name }}</span>
            <span v-if="cred.credential_type === 'ssh_key'" class="inline-flex items-center gap-1 text-xs text-muted-fg">
              <Key :size="10" />
              SSH Key
            </span>
            <span v-else-if="cred.target_path" class="text-xs text-muted-fg">{{ cred.target_path }}</span>
            <span v-else-if="cred.env_var_name" class="text-xs text-muted-fg">{{ cred.env_var_name }}</span>
          </button>
        </div>
        <p v-else class="text-xs text-muted-fg">No credentials available.</p>
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
