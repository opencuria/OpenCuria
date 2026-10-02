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
import { RuntimeType, type Workspace, type WorkspaceUpdateIn } from '@/types'
import { DEFAULT_DESKTOP_HEIGHT, DEFAULT_DESKTOP_WIDTH } from '@/lib/desktopGeometry'
import { readWorkspaceDraft, removeWorkspaceDraft, saveWorkspaceDraft } from '@/lib/workspaceDraft'
import type { WorkspaceDraftFields } from '@/lib/workspaceDraft'

const props = withDefaults(
  defineProps<{
    workspace: Workspace
    size?: 'default' | 'sm'
    disabled?: boolean
    resumeDraftId?: string
    hideTrigger?: boolean
  }>(),
  { hideTrigger: false },
)
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ draftError: [message: string] }>()
const credentialStore = useCredentialStore()
const workspaceStore = useWorkspaceStore()
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
const btnSize = computed(() => (props.size === 'sm' ? ('icon-sm' as const) : ('icon' as const)))
const qemuDefaults = computed(() => {
  const runner = runnerStore.runnerById(props.workspace.runner_id)
  return {
    vcpus: runner?.qemu_default_vcpus ?? 2,
    memoryMb: runner?.qemu_default_memory_mb ?? 4096,
    diskSizeGb: runner?.qemu_default_disk_size_gb ?? 50,
  }
})
const qemuLimits = computed(() => {
  const runner = runnerStore.runnerById(props.workspace.runner_id)
  return runner
    ? {
        minVcpus: runner.qemu_min_vcpus,
        maxVcpus: runner.qemu_max_vcpus,
        minMemoryMb: runner.qemu_min_memory_mb,
        maxMemoryMb: runner.qemu_max_memory_mb,
        minDiskSizeGb: runner.qemu_min_disk_size_gb,
        maxDiskSizeGb: runner.qemu_max_disk_size_gb,
      }
    : {
        minVcpus: 1,
        maxVcpus: 64,
        minMemoryMb: 512,
        maxMemoryMb: 262144,
        minDiskSizeGb: 10,
        maxDiskSizeGb: 2000,
      }
})
function syncForm(workspace: Workspace): void {
  name.value = workspace.name
  selectedCredentialIds.value = [...workspace.credential_ids]
  selectedPluginIds.value = [...workspace.plugin_ids]
  qemuVcpus.value = workspace.qemu_vcpus ?? qemuDefaults.value.vcpus
  qemuMemoryMb.value = workspace.qemu_memory_mb ?? qemuDefaults.value.memoryMb
  qemuDiskSizeGb.value = workspace.qemu_disk_size_gb ?? qemuDefaults.value.diskSizeGb
  desktopWidth.value = workspace.desktop_width || DEFAULT_DESKTOP_WIDTH
  desktopHeight.value = workspace.desktop_height || DEFAULT_DESKTOP_HEIGHT
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
const resourceSelectionValid = computed(() => {
  const widthValid =
    desktopWidth.value >= 800 && desktopWidth.value <= 3840 && desktopWidth.value % 2 === 0
  const heightValid =
    desktopHeight.value >= 600 && desktopHeight.value <= 2160 && desktopHeight.value % 2 === 0
  if (!widthValid || !heightValid) return false
  if (props.workspace.runtime_type !== RuntimeType.QEMU) return true
  return (
    qemuVcpus.value >= qemuLimits.value.minVcpus &&
    qemuVcpus.value <= qemuLimits.value.maxVcpus &&
    qemuMemoryMb.value >= qemuLimits.value.minMemoryMb &&
    qemuMemoryMb.value <= qemuLimits.value.maxMemoryMb &&
    qemuDiskSizeGb.value >= qemuLimits.value.minDiskSizeGb &&
    qemuDiskSizeGb.value <= qemuLimits.value.maxDiskSizeGb
  )
})
function buildPayload(): WorkspaceUpdateIn {
  const payload: WorkspaceUpdateIn = {
    name: name.value.trim(),
    credential_ids: [...selectedCredentialIds.value],
    plugin_ids: [...selectedPluginIds.value],
  }
  if (props.workspace.runtime_type === RuntimeType.QEMU) {
    if (qemuVcpus.value !== (props.workspace.qemu_vcpus ?? qemuDefaults.value.vcpus))
      payload.qemu_vcpus = qemuVcpus.value
    if (qemuMemoryMb.value !== (props.workspace.qemu_memory_mb ?? qemuDefaults.value.memoryMb))
      payload.qemu_memory_mb = qemuMemoryMb.value
    if (
      qemuDiskSizeGb.value !== (props.workspace.qemu_disk_size_gb ?? qemuDefaults.value.diskSizeGb)
    )
      payload.qemu_disk_size_gb = qemuDiskSizeGb.value
  }
  if (desktopWidth.value !== (props.workspace.desktop_width || DEFAULT_DESKTOP_WIDTH))
    payload.desktop_width = desktopWidth.value
  if (desktopHeight.value !== (props.workspace.desktop_height || DEFAULT_DESKTOP_HEIGHT))
    payload.desktop_height = desktopHeight.value
  return payload
}
async function handleSubmit(): Promise<void> {
  if (
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

    <DialogContent class="sm:max-w-xl">
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

          <div v-if="workspace.runtime_type === RuntimeType.QEMU" class="space-y-3">
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
