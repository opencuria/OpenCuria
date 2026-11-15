<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Pencil } from '@lucide/vue'
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
import WorkspacePluginCredentialSelection from './WorkspacePluginCredentialSelection.vue'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'
import { useCredentialStore } from '@/stores/credentials'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { type Workspace, type WorkspaceUpdateIn } from '@/types'
import {
  DEFAULT_DESKTOP_HEIGHT,
  DEFAULT_DESKTOP_WIDTH,
  UNLISTED_RUNNER_QEMU_LIMITS,
  areQemuResourcesValid,
  isDesktopSizeValid,
  qemuResourceLimits,
  workspaceSettingsForm,
  workspaceUpdatePayload,
} from '@opencuria/chat-core/workspaceCreation'
import { readWorkspaceDraft, removeWorkspaceDraft, saveWorkspaceDraft } from '@/lib/workspaceDraft'
import type { WorkspaceDraftFields } from '@/lib/workspaceDraft'

const props = withDefaults(
  defineProps<{
    workspace: Workspace
    size?: 'default' | 'sm'
    disabled?: boolean
    resumeDraftId?: string
    hideTrigger?: boolean
    /** Open with the storage size focused, e.g. from a full-disk hint. */
    focusDisk?: boolean
  }>(),
  { hideTrigger: false },
)
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ draftError: [message: string] }>()
const credentialStore = useCredentialStore()
const workspaceStore = useWorkspaceStore()
watch(
  () => workspaceStore.isWorkspaceTransitioning(props.workspace.id),
  (busy) => {
    if (busy) open.value = false
  },
)
const runnerStore = useRunnerStore()
const authStore = useAuthStore()
const notifications = useNotificationStore()
const router = useRouter()
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const selectedPluginIds = ref<string[]>([])
const pluginSelectionValid = ref(false)
const draftError = ref<string | null>(null)
const restoringDraftId = ref<string | null>(null)
const qemuVcpus = ref(2)
const qemuMemoryMb = ref(4096)
const qemuDiskSizeGb = ref(50)
const desktopWidth = ref(DEFAULT_DESKTOP_WIDTH)
const desktopHeight = ref(DEFAULT_DESKTOP_HEIGHT)
const submitting = ref(false)
let preserveDraftOnClose = false
let resetTimer: ReturnType<typeof setTimeout> | undefined
const diskSizeInput = ref<HTMLInputElement | null>(null)
/** Focus the storage size and keep it in view while plugins and credentials load in. */
function handleOpenAutoFocus(event: Event): void {
  const input = diskSizeInput.value
  if (!props.focusDisk || !input) return
  event.preventDefault()
  input.focus({ preventScroll: true })
  input.select()
  const reveal = () => input.scrollIntoView({ block: 'center' })
  reveal()
  const form = input.form
  if (!form || typeof ResizeObserver === 'undefined') return
  const observer = new ResizeObserver(reveal)
  const settle = () => observer.disconnect()
  observer.observe(form)
  form.addEventListener('wheel', settle, { once: true, passive: true })
  form.addEventListener('touchstart', settle, { once: true, passive: true })
  setTimeout(settle, 2000)
}
const btnSize = computed(() => (props.size === 'sm' ? ('icon-sm' as const) : ('icon' as const)))
const qemuLimits = computed(() =>
  qemuResourceLimits(
    runnerStore.runnerById(props.workspace.runner_id),
    UNLISTED_RUNNER_QEMU_LIMITS,
  ),
)
const qemuDefaults = computed(() => ({
  vcpus: qemuLimits.value.defaultVcpus,
  memoryMb: qemuLimits.value.defaultMemoryMb,
  diskSizeGb: qemuLimits.value.defaultDiskSizeGb,
}))
function syncForm(workspace: Workspace): void {
  const form = workspaceSettingsForm(workspace, qemuLimits.value)
  name.value = form.name
  selectedCredentialIds.value = form.credentialIds
  selectedPluginIds.value = form.pluginIds
  qemuVcpus.value = form.vcpus
  qemuMemoryMb.value = form.memoryMb
  qemuDiskSizeGb.value = form.diskSizeGb
  desktopWidth.value = form.desktopWidth
  desktopHeight.value = form.desktopHeight
}
watch(
  () => props.workspace,
  (workspace) => {
    if (!open.value) syncForm(workspace)
  },
  { immediate: true, deep: true },
)
function restoreDraft(id: string): void {
  if (!authStore.initialized || !authStore.user || !authStore.activeOrganizationId) return
  const result = readWorkspaceDraft(id, {
    userId: authStore.user.id,
    organizationId: authStore.activeOrganizationId,
  })
  if (
    result.status !== 'ok' ||
    result.draft.fields.mode !== 'edit' ||
    result.draft.fields.workspaceId !== props.workspace.id
  ) {
    draftError.value = `Workspace draft could not be restored (${result.status}). Start a new configuration.`
    return
  }
  const fields = result.draft.fields
  restoringDraftId.value = id
  name.value = fields.name
  selectedCredentialIds.value = [...fields.credentialIds]
  selectedPluginIds.value = [...fields.pluginIds]
  qemuVcpus.value = fields.qemuVcpus ?? qemuDefaults.value.vcpus
  qemuMemoryMb.value = fields.qemuMemoryMb ?? qemuDefaults.value.memoryMb
  qemuDiskSizeGb.value = fields.qemuDiskSizeGb ?? qemuDefaults.value.diskSizeGb
  desktopWidth.value = fields.desktopWidth ?? props.workspace.desktop_width
  desktopHeight.value = fields.desktopHeight ?? props.workspace.desktop_height
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
onUnmounted(() => {
  if (resetTimer) clearTimeout(resetTimer)
})
async function handleOpen(): Promise<void> {
  if (props.disabled) return
  open.value = true
  if (restoringDraftId.value && props.resumeDraftId === restoringDraftId.value) {
    await Promise.all([
      credentialStore.fetchCredentials(),
      runnerStore.runners.length ? Promise.resolve() : runnerStore.fetchRunners(),
    ])
    return
  }
  syncForm(props.workspace)
  await Promise.all([
    credentialStore.fetchCredentials(),
    runnerStore.runners.length ? Promise.resolve() : runnerStore.fetchRunners(),
  ])
}
const resourceSelectionValid = computed(
  () =>
    isDesktopSizeValid(desktopWidth.value, desktopHeight.value) &&
    areQemuResourcesValid(
      { vcpus: qemuVcpus.value, memoryMb: qemuMemoryMb.value, diskSizeGb: qemuDiskSizeGb.value },
      qemuLimits.value,
    ),
)
function buildPayload(): WorkspaceUpdateIn {
  return workspaceUpdatePayload(
    props.workspace,
    {
      name: name.value,
      credentialIds: selectedCredentialIds.value,
      pluginIds: selectedPluginIds.value,
      vcpus: qemuVcpus.value,
      memoryMb: qemuMemoryMb.value,
      diskSizeGb: qemuDiskSizeGb.value,
      desktopWidth: desktopWidth.value,
      desktopHeight: desktopHeight.value,
    },
    qemuLimits.value,
  )
}
async function handleSubmit(): Promise<void> {
  if (
    submitting.value ||
    !name.value.trim() ||
    !pluginSelectionValid.value ||
    !resourceSelectionValid.value ||
    props.disabled
  )
    return
  submitting.value = true
  try {
    const saved = await workspaceStore.updateWorkspace(props.workspace.id, buildPayload())
    if (!saved) return
    if (restoringDraftId.value) removeWorkspaceDraft(restoringDraftId.value)
    restoringDraftId.value = null
    await workspaceStore.fetchWorkspaceDetail(props.workspace.id)
    syncForm(props.workspace)
    open.value = false
  } finally {
    submitting.value = false
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
    draftError.value = null
    syncForm(props.workspace)
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
    mode: 'edit',
    workspaceId: props.workspace.id,
    name: name.value,
    credentialIds: [...selectedCredentialIds.value],
    pluginIds: [...selectedPluginIds.value],
    qemuVcpus: qemuVcpus.value,
    qemuMemoryMb: qemuMemoryMb.value,
    qemuDiskSizeGb: qemuDiskSizeGb.value,
    desktopWidth: desktopWidth.value,
    desktopHeight: desktopHeight.value,
  }
  const result = saveWorkspaceDraft(
    fields,
    { userId: authStore.user?.id ?? '', organizationId: authStore.activeOrganizationId ?? '' },
    `/workspaces/${props.workspace.id}`,
  )
  if (result.error) {
    draftError.value = result.error
    emit('draftError', result.error)
    notifications.error('Draft not saved', result.error)
    return
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
const canSave = computed(
  () =>
    !submitting.value &&
    !props.disabled &&
    !!name.value.trim() &&
    pluginSelectionValid.value &&
    resourceSelectionValid.value,
)

defineExpose({ show: handleOpen })
</script>

<template>
  <Dialog :open="open" @update:open="(value) => (value ? handleOpen() : handleClose())">
    <DialogTrigger v-if="!hideTrigger" as-child>
      <Button
        variant="ghost"
        :size="btnSize"
        title="Edit workspace"
        :disabled="props.disabled"
        @click.stop="handleOpen"
      >
        <Pencil :size="14" />
      </Button>
    </DialogTrigger>

    <DialogContent class="sm:max-w-xl" @open-auto-focus="handleOpenAutoFocus">
      <DialogHeader>
        <DialogTitle>Edit Workspace</DialogTitle>
        <DialogDescription
          >Update the workspace name, desktop size, credentials, and plugins.</DialogDescription
        >
      </DialogHeader>

      <DialogBody>
        <form id="edit-workspace-form" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
            <Input
              v-model="name"
              :disabled="submitting || props.disabled"
              placeholder="Workspace name"
            />
          </div>

          <WorkspacePluginCredentialSelection
            v-model:plugin-ids="selectedPluginIds"
            v-model:credential-ids="selectedCredentialIds"
            :disabled="submitting || props.disabled"
            :active="open"
            @validity-change="pluginSelectionValid = $event"
            @add-credential="navigateToCredentials"
            @reconnect-credential="(id) => navigateToCredentials(undefined, id)"
          />
          <p v-if="draftError" class="text-xs text-destructive" role="alert">{{ draftError }}</p>
          <p v-if="!resourceSelectionValid" class="text-xs text-destructive" role="alert">
            Restore or adjust resource values to fit the workspace runner's limits.
          </p>

          <div class="space-y-3">
            <label class="text-sm font-medium text-foreground block">Desktop size</label>
            <p class="text-xs text-muted-foreground">Applies the next time the desktop starts.</p>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="text-sm font-medium text-muted-foreground mb-1 block">Width</label>
                <Input
                  v-model.number="desktopWidth"
                  :disabled="submitting || props.disabled"
                  type="number"
                  min="800"
                  max="3840"
                  step="2"
                />
              </div>
              <div>
                <label class="text-sm font-medium text-muted-foreground mb-1 block">Height</label>
                <Input
                  v-model.number="desktopHeight"
                  :disabled="submitting || props.disabled"
                  type="number"
                  min="600"
                  max="2160"
                  step="2"
                />
              </div>
            </div>
          </div>

          <div class="space-y-3">
            <label class="text-sm font-medium text-foreground block">QEMU resources</label>

            <div>
              <label class="text-sm font-medium text-muted-foreground mb-1 block">vCPU</label>
              <input
                v-model.number="qemuVcpus"
                :disabled="submitting || props.disabled"
                type="range"
                class="w-full accent-primary"
                :min="qemuLimits.minVcpus"
                :max="qemuLimits.maxVcpus"
                step="1"
              />
              <input
                v-model.number="qemuVcpus"
                :disabled="submitting || props.disabled"
                type="number"
                class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary"
                :min="qemuLimits.minVcpus"
                :max="qemuLimits.maxVcpus"
                step="1"
              />
            </div>

            <div>
              <label class="text-sm font-medium text-muted-foreground mb-1 block">RAM (MiB)</label>
              <input
                v-model.number="qemuMemoryMb"
                :disabled="submitting || props.disabled"
                type="range"
                class="w-full accent-primary"
                :min="qemuLimits.minMemoryMb"
                :max="qemuLimits.maxMemoryMb"
                step="256"
              />
              <input
                v-model.number="qemuMemoryMb"
                :disabled="submitting || props.disabled"
                type="number"
                class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary"
                :min="qemuLimits.minMemoryMb"
                :max="qemuLimits.maxMemoryMb"
                step="256"
              />
            </div>

            <div>
              <label class="text-sm font-medium text-muted-foreground mb-1 block"
                >Storage (GiB)</label
              >
              <input
                v-model.number="qemuDiskSizeGb"
                :disabled="submitting || props.disabled"
                type="range"
                class="w-full accent-primary"
                :min="qemuLimits.minDiskSizeGb"
                :max="qemuLimits.maxDiskSizeGb"
                step="1"
              />
              <input
                ref="diskSizeInput"
                v-model.number="qemuDiskSizeGb"
                :disabled="submitting || props.disabled"
                type="number"
                class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary"
                :min="qemuLimits.minDiskSizeGb"
                :max="qemuLimits.maxDiskSizeGb"
                step="1"
              />
            </div>
          </div>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="submitting" @click="handleClose"
          >Cancel</Button
        >
        <Button
          type="submit"
          form="edit-workspace-form"
          data-testid="edit-workspace-save"
          :disabled="!canSave"
        >
          {{ submitting ? 'Saving…' : 'Save Changes' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
