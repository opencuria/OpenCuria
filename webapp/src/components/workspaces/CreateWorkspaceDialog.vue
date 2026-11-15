<script setup lang="ts">
import { onMounted, computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import WorkspacePluginCredentialSelection from './WorkspacePluginCredentialSelection.vue'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'
import { readWorkspaceDraft, removeWorkspaceDraft, saveWorkspaceDraft } from '@/lib/workspaceDraft'
import type { WorkspaceDraftFields } from '@/lib/workspaceDraft'
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { useImageStore } from '@/stores/images'
import {
  areQemuResourcesValid,
  compatibleRunners,
  defaultWorkspaceImageOption,
  definitionArtifactForRunner,
  fitQemuResources,
  qemuResourceLimits,
  type WorkspaceImageOption,
} from '@opencuria/chat-core/workspaceCreation'
import { CloudAlert, Layers, Plus, X } from '@lucide/vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { OPEN_SETTINGS_EVENT, type SettingsTabId } from '@/components/settings/settingsTabs'
import { useWorkspaceImageReadiness } from '@/composables/useWorkspaceImageReadiness'
import type { ImageArtifact, RunnerImageBuild } from '@/types'

type SelectableImageOption = WorkspaceImageOption<RunnerImageBuild, ImageArtifact>

const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const imageStore = useImageStore()
const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const notifications = useNotificationStore()

const props = withDefaults(defineProps<{ resumeDraftId?: string; hideTrigger?: boolean }>(), {
  hideTrigger: false,
})
const emit = defineEmits<{
  created: [workspaceId: string | null]
  draftError: [message: string]
  handoff: []
}>()

const open = defineModel<boolean>('open', { default: false })
const activeTab = ref<'basic' | 'advanced'>('basic')
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const selectedPluginIds = ref<string[]>([])
const pluginSelectionValid = ref(false)
const draftError = ref<string | null>(null)
const restoringDraftId = ref<string | null>(null)
let preserveDraftOnClose = false
let resetTimer: ReturnType<typeof setTimeout> | undefined
const runnerId = ref('')
const qemuVcpus = ref(2)
const qemuMemoryMb = ref(4096)
const qemuDiskSizeGb = ref(50)
const repoInput = ref('')
const repos = ref<string[]>([])
const submitting = ref(false)

// Image-based creation
const selectedImageValue = ref('')

function identity() {
  return { userId: authStore.user?.id ?? '', organizationId: authStore.activeOrganizationId ?? '' }
}
const restorationPending = ref(false)
function restoreDraft(id: string): void {
  if (!authStore.initialized || !authStore.user || !authStore.activeOrganizationId) return
  const result = readWorkspaceDraft(id, identity())
  if (result.status !== 'ok' || result.draft.fields.mode !== 'create') {
    draftError.value = `Workspace draft could not be restored (${result.status}). Start a new configuration.`
    return
  }
  const fields = result.draft.fields
  restorationPending.value = true
  restoringDraftId.value = id
  name.value = fields.name
  selectedCredentialIds.value = [...fields.credentialIds]
  selectedPluginIds.value = [...fields.pluginIds]
  repos.value = [...(fields.repos ?? [])]
  runnerId.value = fields.runnerId ?? ''
  qemuVcpus.value = fields.qemuVcpus ?? 2
  qemuMemoryMb.value = fields.qemuMemoryMb ?? 4096
  qemuDiskSizeGb.value = fields.qemuDiskSizeGb ?? 50
  selectedImageValue.value =
    fields.imageValue ?? (fields.imageArtifactId ? `captured:${fields.imageArtifactId}` : '')
  open.value = true
}
watch(
  [
    () => props.resumeDraftId,
    () => authStore.initialized,
    () => authStore.user?.id,
    () => authStore.activeOrganizationId,
  ],
  ([id, initialized, userId, organizationId]) => {
    if (id && initialized && userId !== undefined && organizationId) restoreDraft(id)
  },
  { immediate: true },
)

const imageReadiness = useWorkspaceImageReadiness()
const groupedImageOptions = imageReadiness.imageOptions
/**
 * Nothing to start from: explain instead of showing a form that cannot be
 * submitted. A restored draft keeps its form so the user's input stays visible.
 */
const imageUnavailable = computed<'error' | 'empty' | null>(() => {
  if (!imageReadiness.checked.value || imageReadiness.ready.value || restoringDraftId.value)
    return null
  return imageReadiness.error.value ? 'error' : 'empty'
})
const imageSetupTab = computed<SettingsTabId>(() =>
  imageReadiness.onlineRunner.value ? 'image-definitions' : 'runners',
)

function openImageSetup(): void {
  const tab = imageSetupTab.value
  handleClose()
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab } }))
}

const imageOptions = computed(() => {
  const definitionOptions = groupedImageOptions.value.definitions.map((option) => ({
    ...option,
    label: `● ${option.label}`,
  }))
  const capturedOptions = groupedImageOptions.value.captured.map((option) => ({
    ...option,
    label: `○ ${option.label}`,
  }))

  const options = [
    ...(definitionOptions.length
      ? [{ value: '__group_definitions__', label: '── Organization Images ──────────────' }]
      : []),
    ...definitionOptions,
    ...(capturedOptions.length
      ? [{ value: '__group_captured__', label: '── Captured Images ──────────────────' }]
      : []),
    ...capturedOptions,
  ]

  return options
})

const selectableOptionsByValue = computed<Record<string, SelectableImageOption>>(() => {
  const map: Record<string, SelectableImageOption> = {}
  for (const option of imageOptions.value) {
    if (option.value.startsWith('__group_')) continue
    map[option.value] = option as SelectableImageOption
  }
  return map
})

const selectedImageOption = computed<SelectableImageOption | null>(() => {
  if (!selectedImageValue.value) return null
  return selectableOptionsByValue.value[selectedImageValue.value] ?? null
})

const isFromImage = computed(() => !!selectedImageOption.value)
const isCapturedClone = computed(() => selectedImageOption.value?.kind === 'captured')

const compatibleRunnerOptions = computed(() =>
  compatibleRunners(selectedImageOption.value, runnerStore.runners).map((runner) => ({
    value: runner.id,
    label: runner.name || runner.id.slice(0, 8),
  })),
)

const selectedDefinitionArtifactId = computed<string | null>(() =>
  definitionArtifactForRunner(
    selectedImageOption.value,
    runnerId.value || compatibleRunnerOptions.value[0]?.value,
  ),
)

watch(selectedImageOption, (option, previousOption) => {
  if (!restorationPending.value && previousOption && previousOption.value !== option?.value) {
    selectedCredentialIds.value = []
    selectedPluginIds.value = []
  }
})

watch(
  compatibleRunnerOptions,
  (runnerOptions) => {
    const compatibleRunnerIds = runnerOptions.map((entry) => entry.value)
    if (!compatibleRunnerIds.length) {
      if (!restorationPending.value) runnerId.value = ''
      return
    } else if (!compatibleRunnerIds.includes(runnerId.value)) {
      if (restorationPending.value) return
      runnerId.value = compatibleRunnerIds[0] || ''
    }
  },
  { immediate: true },
)

watch(
  [() => open.value, imageOptions],
  () => {
    if (!open.value) return
    const nextDefault = defaultWorkspaceImageOption(groupedImageOptions.value)
    if (!nextDefault) {
      if (!restoringDraftId.value) selectedImageValue.value = ''
      return
    }
    if (!selectedImageValue.value && !restoringDraftId.value) {
      selectedImageValue.value = nextDefault.value
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (resetTimer) clearTimeout(resetTimer)
})

onMounted(async () => {
  await imageReadiness.check()
  if (props.resumeDraftId) {
    await nextTick()
    restorationPending.value = false
  }
})

const effectiveQemuRunner = computed(() => {
  if (runnerId.value) {
    return runnerStore.runners.find((runner) => runner.id === runnerId.value) ?? null
  }
  return null
})

const advancedRunnerOptions = computed(() => {
  if (isFromImage.value) {
    return compatibleRunnerOptions.value
  }

  return runnerStore.onlineRunners.map((runner) => ({
    value: runner.id,
    label: runner.name || runner.id.slice(0, 8),
  }))
})

const qemuLimits = computed(() => qemuResourceLimits(effectiveQemuRunner.value))

watch(
  [() => open.value, qemuLimits, selectedImageValue],
  ([, limits], previous) => {
    if (!open.value || isCapturedClone.value) return

    // A value still at the previous defaults was never customized, so it
    // follows the effective runner's defaults instead of a stale fallback.
    const previousLimits = previous?.[1] ?? limits
    if (restorationPending.value) return
    const fitted = fitQemuResources(
      {
        vcpus: qemuVcpus.value,
        memoryMb: qemuMemoryMb.value,
        diskSizeGb: qemuDiskSizeGb.value,
      },
      previousLimits,
      limits,
    )
    qemuVcpus.value = fitted.vcpus
    qemuMemoryMb.value = fitted.memoryMb
    qemuDiskSizeGb.value = fitted.diskSizeGb
  },
  { immediate: true },
)

function addRepo(): void {
  const trimmed = repoInput.value.trim()
  if (trimmed && !repos.value.includes(trimmed)) {
    repos.value.push(trimmed)
    repoInput.value = ''
  }
}

function removeRepo(index: number): void {
  repos.value.splice(index, 1)
}

function handleRepoKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter') {
    e.preventDefault()
    addRepo()
  }
}

async function handleSubmit(): Promise<void> {
  if (!name.value.trim() || !pluginSelectionValid.value) return
  const selectedOption = selectedImageOption.value
  if (!selectedOption) return

  submitting.value = true

  let success: boolean
  let createdWorkspaceId: string | null = null
  if (selectedOption.kind === 'captured') {
    const data = {
      name: name.value.trim(),
      credential_ids: selectedCredentialIds.value,
      plugin_ids: selectedPluginIds.value,
    }
    const workspaceId = await imageStore.createWorkspaceFromCapturedImage(
      selectedOption.capturedImageId,
      data,
    )
    success = !!workspaceId
    createdWorkspaceId = workspaceId
  } else {
    addRepo()

    const resolvedImageId = selectedDefinitionArtifactId.value

    if (!resolvedImageId) {
      submitting.value = false
      return
    }

    const beforeIds = new Set(workspaceStore.workspaces.map((entry) => entry.id))
    success = await workspaceStore.createWorkspace({
      name: name.value.trim(),
      repos: repos.value,
      credential_ids: selectedCredentialIds.value,
      plugin_ids: selectedPluginIds.value,
      runner_id: runnerId.value || null,
      image_id: resolvedImageId,

      qemu_vcpus: qemuVcpus.value,
      qemu_memory_mb: qemuMemoryMb.value,
      qemu_disk_size_gb: qemuDiskSizeGb.value,
    })
    if (success) {
      const created = workspaceStore.workspaces.find((entry) => !beforeIds.has(entry.id))
      createdWorkspaceId = created?.id ?? null
    }
  }

  submitting.value = false

  if (success) {
    if (restoringDraftId.value) removeWorkspaceDraft(restoringDraftId.value)
    restoringDraftId.value = null
    handleClose()
    emit('created', createdWorkspaceId)
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
    activeTab.value = 'basic'
    name.value = ''
    selectedCredentialIds.value = []
    selectedPluginIds.value = []
    runnerId.value = ''
    qemuVcpus.value = 2
    qemuMemoryMb.value = 4096
    qemuDiskSizeGb.value = 50
    repoInput.value = ''
    repos.value = []
    selectedImageValue.value = ''
    draftError.value = null
    resetTimer = undefined
  }, 200)
}
watch(open, (isOpen) => {
  if (isOpen && resetTimer) {
    clearTimeout(resetTimer)
    resetTimer = undefined
  }
  // The dialog stays mounted between uses; runners and images may have changed.
  if (isOpen) void imageReadiness.check()
})

async function navigateToCredentials(serviceId?: string, reconnectId?: string): Promise<void> {
  addRepo()
  const fields: WorkspaceDraftFields = {
    mode: 'create',
    name: name.value,
    credentialIds: [...selectedCredentialIds.value],
    pluginIds: [...selectedPluginIds.value],
    repos: [...repos.value],
    runnerId: runnerId.value,
    qemuVcpus: qemuVcpus.value,
    qemuMemoryMb: qemuMemoryMb.value,
    qemuDiskSizeGb: qemuDiskSizeGb.value,
    imageValue: selectedImageValue.value,
    imageArtifactId:
      selectedImageOption.value?.kind === 'captured'
        ? selectedImageOption.value.imageArtifact.id
        : undefined,
  }
  const result = saveWorkspaceDraft(
    fields,
    identity(),
    route.path === '/workspaces' || /^\/workspaces\/[\w-]+$/.test(route.path)
      ? (route.path as '/workspaces' | `/workspaces/${string}`)
      : '/',
  )
  if (result.error) {
    draftError.value = result.error
    emit('draftError', result.error)
    notifications.error('Draft not saved', result.error)
    return
  }
  restoringDraftId.value = result.id
  preserveDraftOnClose = true
  emit('handoff')
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

const resourceSelectionValid = computed(
  () =>
    isCapturedClone.value ||
    areQemuResourcesValid(
      { vcpus: qemuVcpus.value, memoryMb: qemuMemoryMb.value, diskSizeGb: qemuDiskSizeGb.value },
      qemuLimits.value,
    ),
)
const isValid = computed(
  () =>
    name.value.trim().length > 0 &&
    !!selectedImageOption.value &&
    pluginSelectionValid.value &&
    resourceSelectionValid.value &&
    (selectedImageOption.value.kind === 'captured' || !!selectedDefinitionArtifactId.value),
)
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (v ? (open = true) : handleClose())">
    <DialogTrigger v-if="!hideTrigger" as-child>
      <slot><Button @click="open = true"><Plus />Create workspace</Button></slot>
    </DialogTrigger>

    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>Create Workspace</DialogTitle>
        <DialogDescription>
          Provision a new workspace VM. Start a harness session to run the agent.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <EmptyState
          v-if="imageUnavailable === 'error'"
          :icon="CloudAlert"
          title="Couldn’t check available images"
          :description="imageReadiness.error.value ?? undefined"
          data-testid="create-workspace-image-error"
        >
          <template #action>
            <Button variant="outline" size="sm" @click="imageReadiness.check()">Try again</Button>
          </template>
        </EmptyState>
        <EmptyState
          v-else-if="imageUnavailable === 'empty'"
          :icon="Layers"
          title="No workspace image yet"
          :description="
            authStore.isAdmin
              ? imageSetupTab === 'runners'
                ? 'Workspaces start from an image, built on a runner. Connect a runner first.'
                : 'Workspaces start from an image. Build one on your runner first.'
              : 'Workspaces start from an image. You can create one as soon as an image is ready.'
          "
          data-testid="create-workspace-no-image"
        >
          <template v-if="authStore.isAdmin" #action>
            <Button size="sm" data-testid="create-workspace-image-setup" @click="openImageSetup">
              {{ imageSetupTab === 'runners' ? 'Open runners' : 'Open image definitions' }}
            </Button>
          </template>
        </EmptyState>
        <form
          v-else
          id="create-workspace-form"
          class="flex flex-col gap-4"
          @submit.prevent="handleSubmit"
        >
          <div class="rounded-md border border-border bg-muted/50 p-1">
            <div class="grid grid-cols-2 gap-1">
              <Button
                type="button"
                size="sm"
                :variant="activeTab === 'basic' ? 'secondary' : 'ghost'"
                @click="activeTab = 'basic'"
              >
                Basic
              </Button>
              <Button
                type="button"
                size="sm"
                :variant="activeTab === 'advanced' ? 'secondary' : 'ghost'"
                @click="activeTab = 'advanced'"
              >
                Advanced settings
              </Button>
            </div>
          </div>

          <p v-if="activeTab === 'basic'" class="text-xs text-muted-foreground -mt-1">
            In most cases you can create directly from this tab. Advanced settings are optional.
          </p>

          <!-- Workspace name -->
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
            <Input v-model="name" placeholder="My workspace" />
          </div>

          <!-- Image selection -->
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">
              Select image
              <span class="text-muted-foreground font-normal">(required)</span>
            </label>
            <Select v-model="selectedImageValue">
              <SelectTrigger data-testid="create-image-select">
                <SelectValue placeholder="Select image" />
              </SelectTrigger>
              <SelectContent>
                <template v-for="opt in imageOptions" :key="opt.value">
                  <SelectLabel v-if="opt.value.startsWith('__group_')">{{ opt.label }}</SelectLabel>
                  <SelectItem v-else :value="opt.value">{{ opt.label }}</SelectItem>
                </template>
              </SelectContent>
            </Select>
            <p
              v-if="imageReadiness.checked.value && !selectedImageOption"
              class="text-xs text-destructive mt-1"
            >
              No active organization or captured images are available.
            </p>
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
          <p v-if="!resourceSelectionValid" class="text-xs text-destructive" role="alert">
            The restored QEMU resource values are outside this runner's limits. Adjust them before
            saving.
          </p>

          <div v-if="!isCapturedClone">
            <label class="text-sm font-medium text-foreground mb-1.5 block"
              >Repositories <span class="text-muted-foreground font-normal">(optional)</span></label
            >
            <div class="flex gap-2">
              <Input
                v-model="repoInput"
                placeholder="https://github.com/owner/repo"
                class="flex-1"
                @keydown="handleRepoKeydown"
              />
              <Button type="button" variant="outline" @click="addRepo">Add</Button>
            </div>
            <div v-if="repos.length" class="flex flex-wrap gap-2 mt-2">
              <span
                v-for="(repo, i) in repos"
                :key="repo"
                class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-sm bg-muted text-foreground"
              >
                {{ repo.split('/').slice(-1)[0] || repo }}
                <button
                  type="button"
                  class="text-muted-foreground hover:text-foreground cursor-pointer"
                  @click="removeRepo(i)"
                >
                  <X :size="12" />
                </button>
              </span>
            </div>
          </div>

          <div v-if="activeTab === 'advanced'">
            <div
              v-if="isCapturedClone"
              class="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground"
            >
              Captured image clones keep runner and resources from the image. Advanced options are
              disabled.
            </div>

            <div v-else class="space-y-4">
              <div class="rounded-md border border-border bg-muted/50 p-3">
                <label class="text-sm font-medium text-foreground mb-1.5 block">Runner</label>
                <Select v-model="runnerId" :disabled="advancedRunnerOptions.length <= 1">
                  <SelectTrigger>
                    <SelectValue placeholder="Select runner" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem
                      v-for="opt in advancedRunnerOptions"
                      :key="opt.value"
                      :value="opt.value"
                    >
                      {{ opt.label }}
                    </SelectItem>
                  </SelectContent>
                </Select>
                <p class="text-xs text-muted-foreground mt-1">
                  {{
                    isFromImage
                      ? 'Only online runners with the selected image are available.'
                      : 'Only online runners are available.'
                  }}
                </p>
              </div>

              <div class="space-y-3 rounded-md border border-border bg-muted/50 p-3">
                <p class="text-sm font-medium text-foreground">QEMU resources</p>

                <div>
                  <label class="text-sm font-medium text-muted-foreground mb-1 block">vCPU</label>
                  <input
                    v-model.number="qemuVcpus"
                    type="range"
                    class="w-full accent-primary"
                    :min="qemuLimits.minVcpus"
                    :max="qemuLimits.maxVcpus"
                    step="1"
                  />
                  <input
                    v-model.number="qemuVcpus"
                    type="number"
                    class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary"
                    :min="qemuLimits.minVcpus"
                    :max="qemuLimits.maxVcpus"
                    step="1"
                  />
                  <div class="flex justify-between text-xs text-muted-foreground mt-1">
                    <span>{{ qemuLimits.minVcpus }}</span>
                    <span>{{ qemuLimits.maxVcpus }}</span>
                  </div>
                </div>

                <div>
                  <label class="text-sm font-medium text-muted-foreground mb-1 block"
                    >RAM (MiB)</label
                  >
                  <input
                    v-model.number="qemuMemoryMb"
                    type="range"
                    class="w-full accent-primary"
                    :min="qemuLimits.minMemoryMb"
                    :max="qemuLimits.maxMemoryMb"
                    step="256"
                  />
                  <input
                    v-model.number="qemuMemoryMb"
                    type="number"
                    class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary"
                    :min="qemuLimits.minMemoryMb"
                    :max="qemuLimits.maxMemoryMb"
                    step="256"
                  />
                  <div class="flex justify-between text-xs text-muted-foreground mt-1">
                    <span>{{ qemuLimits.minMemoryMb }}</span>
                    <span>{{ qemuLimits.maxMemoryMb }}</span>
                  </div>
                </div>

                <div>
                  <label class="text-sm font-medium text-muted-foreground mb-1 block"
                    >Storage (GiB)</label
                  >
                  <input
                    v-model.number="qemuDiskSizeGb"
                    type="range"
                    class="w-full accent-primary"
                    :min="qemuLimits.minDiskSizeGb"
                    :max="qemuLimits.maxDiskSizeGb"
                    step="1"
                  />
                  <input
                    v-model.number="qemuDiskSizeGb"
                    type="number"
                    class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary"
                    :min="qemuLimits.minDiskSizeGb"
                    :max="qemuLimits.maxDiskSizeGb"
                    step="1"
                  />
                  <div class="flex justify-between text-xs text-muted-foreground mt-1">
                    <span>{{ qemuLimits.minDiskSizeGb }}</span>
                    <span>{{ qemuLimits.maxDiskSizeGb }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" @click="handleClose">{{
          imageUnavailable ? 'Close' : 'Cancel'
        }}</Button>
        <Button
          v-if="!imageUnavailable"
          type="submit"
          form="create-workspace-form"
          :disabled="!isValid || submitting"
        >
          {{
            submitting
              ? selectedImageOption?.kind === 'captured'
                ? 'Cloning…'
                : 'Creating…'
              : selectedImageOption?.kind === 'captured'
                ? 'Clone from Image'
                : 'Create'
          }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
