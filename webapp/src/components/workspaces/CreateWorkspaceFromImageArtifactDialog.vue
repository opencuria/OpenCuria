<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'
import WorkspacePluginCredentialSelection from './WorkspacePluginCredentialSelection.vue'
import { readWorkspaceDraft, removeWorkspaceDraft, saveWorkspaceDraft } from '@/lib/workspaceDraft'
import type { WorkspaceDraftFields } from '@/lib/workspaceDraft'
import type { ImageArtifact } from '@/types'

const props = defineProps<{
  imageArtifact: ImageArtifact
  /** Create from the captured image so the backend always picks its latest version. */
  capturedImageId?: string
  disabled?: boolean
}>()
const route = useRoute()
const router = useRouter()
const imageArtifactStore = useImageArtifactStore()
const authStore = useAuthStore()
const notifications = useNotificationStore()
const open = ref(false)
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const selectedPluginIds = ref<string[]>([])
const pluginSelectionValid = ref(false)
const draftError = ref<string | null>(null)
const restoringDraftId = ref<string | null>(null)
const submitting = ref(false)
let resetTimer: ReturnType<typeof setTimeout> | undefined
let preserveDraftOnClose = false

const isValid = computed(() => name.value.trim().length > 0 && pluginSelectionValid.value)

function identity() {
  return {
    userId: authStore.user?.id ?? '',
    organizationId: authStore.activeOrganizationId ?? '',
  }
}
function restoreDraft(id: string): void {
  if (!authStore.initialized || !authStore.user || !authStore.activeOrganizationId) return
  const result = readWorkspaceDraft(id, identity())
  if (
    result.status !== 'ok' ||
    result.draft.fields.mode !== 'create' ||
    result.draft.fields.imageArtifactId !== props.imageArtifact.id
  ) {
    draftError.value = `Workspace draft could not be restored (${result.status}). Start a new configuration.`
    return
  }
  restoringDraftId.value = id
  name.value = result.draft.fields.name
  selectedCredentialIds.value = [...result.draft.fields.credentialIds]
  selectedPluginIds.value = [...result.draft.fields.pluginIds]
  open.value = true
}
watch(
  () => route.query.resume_workspace,
  (id) => {
    if (typeof id === 'string') restoreDraft(id)
  },
  { immediate: true },
)

onMounted(async () => {
  await Promise.all([
    imageArtifactStore.loading ? Promise.resolve() : imageArtifactStore.fetchImageArtifacts(),
  ])
})
onUnmounted(() => {
  if (resetTimer) clearTimeout(resetTimer)
})

async function handleSubmit(): Promise<void> {
  if (props.disabled || !isValid.value) return
  submitting.value = true
  const previousDraftId = restoringDraftId.value
  const data = {
    name: name.value.trim(),
    credential_ids: selectedCredentialIds.value,
    plugin_ids: selectedPluginIds.value,
  }
  const workspaceId = props.capturedImageId
    ? await imageArtifactStore.createWorkspaceFromCapturedImage(props.capturedImageId, data)
    : await imageArtifactStore.createWorkspaceFromImageArtifact(props.imageArtifact.id, data)
  submitting.value = false
  if (workspaceId) {
    if (previousDraftId) removeWorkspaceDraft(previousDraftId)
    restoringDraftId.value = null
    handleClose()
    await router.push(`/workspaces/${workspaceId}`)
  }
}

function handleClose(): void {
  open.value = false
  if (resetTimer) clearTimeout(resetTimer)
  if (!preserveDraftOnClose && restoringDraftId.value) removeWorkspaceDraft(restoringDraftId.value)
  if (!preserveDraftOnClose) restoringDraftId.value = null
  preserveDraftOnClose = false
  resetTimer = setTimeout(() => {
    if (open.value) return
    name.value = ''
    selectedCredentialIds.value = []
    selectedPluginIds.value = []
    draftError.value = null
    resetTimer = undefined
  }, 200)
}
watch(open, (value) => {
  if (value && resetTimer) {
    clearTimeout(resetTimer)
    resetTimer = undefined
  }
})

async function navigateToCredentials(serviceId?: string, reconnectId?: string): Promise<void> {
  const fields: WorkspaceDraftFields = {
    mode: 'create',
    name: name.value,
    credentialIds: [...selectedCredentialIds.value],
    pluginIds: [...selectedPluginIds.value],
    imageValue: `captured:${props.imageArtifact.id}`,
    imageArtifactId: props.imageArtifact.id,
  }
  const result = saveWorkspaceDraft(
    fields,
    identity(),
    route.path === '/workspaces' ? '/workspaces' : '/',
  )
  if (result.error) {
    draftError.value = result.error
    notifications.error('Draft not saved', result.error)
    return
  }
  if (restoringDraftId.value && restoringDraftId.value !== result.id) {
    removeWorkspaceDraft(restoringDraftId.value)
  }
  restoringDraftId.value = result.id
  preserveDraftOnClose = true
  handleClose()
  restoringDraftId.value = null
  await router.push({
    path: '/',
    query: {
      settings: 'credentials',
      ...(serviceId ? { add_credential: serviceId } : {}),
      ...(reconnectId ? { reconnect_credential: reconnectId } : {}),
      workspace_draft: result.id,
    },
  })
}
</script>

<template>
  <Dialog :open="open" @update:open="(value) => (value ? (open = true) : handleClose())">
    <DialogTrigger as-child><slot /></DialogTrigger>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Clone Workspace from Image</DialogTitle>
        <DialogDescription>
          Creates a new workspace from this image. Choose plugins and credentials explicitly; image
          contents do not retain credential associations.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        <form id="clone-from-image-form" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
          <div class="rounded-md border border-border bg-muted/50 p-3 text-sm">
            <div class="mb-1 font-medium text-foreground">
              {{ props.imageArtifact.name }}
              <span v-if="props.imageArtifact.version" class="text-muted-foreground"
                >· v{{ props.imageArtifact.version }} (latest)</span
              >
            </div>
            <div class="text-xs text-muted-foreground">
              Only credentials and plugins selected below will be attached.
            </div>
          </div>
          <div>
            <label class="mb-1.5 block text-sm font-medium text-foreground"
              >New workspace name</label
            >
            <Input v-model="name" placeholder="e.g. feature-x" />
          </div>
          <WorkspacePluginCredentialSelection
            v-model:plugin-ids="selectedPluginIds"
            v-model:credential-ids="selectedCredentialIds"
            :active="open"
            @validity-change="pluginSelectionValid = $event"
            @add-credential="navigateToCredentials"
            @reconnect-credential="(id) => navigateToCredentials(undefined, id)"
          />
          <p v-if="draftError" class="text-xs text-destructive" role="alert">{{ draftError }}</p>
          <p v-if="!isValid" class="text-xs text-destructive">
            Choose all required credentials and resolve unavailable selections before cloning.
          </p>
        </form>
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" type="button" :disabled="submitting" @click="handleClose"
          >Cancel</Button
        >
        <Button
          type="submit"
          form="clone-from-image-form"
          :disabled="!isValid || submitting || props.disabled"
        >
          {{ submitting ? 'Cloning…' : 'Clone Workspace' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
