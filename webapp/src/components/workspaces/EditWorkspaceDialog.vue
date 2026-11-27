<!--
  EditWorkspaceDialog — workspace settings as a small navigation stack: the
  main page holds the name and resources and links to "Plugins & Credentials"
  (plugins and credentials), which slides in as a sub page. Both pages stay
  mounted so the selection keeps validating and summarizing while hidden.
-->
<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { AlertCircle, ChevronLeft, ChevronRight, Pencil, Puzzle, RotateCw } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
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
import { WorkspaceStatus, type Workspace, type WorkspaceUpdateIn } from '@/types'
import {
  DEFAULT_DESKTOP_HEIGHT,
  DEFAULT_DESKTOP_WIDTH,
  DESKTOP_SIZE_LIMITS,
  UNLISTED_RUNNER_QEMU_LIMITS,
  areQemuResourcesValid,
  isDesktopSizeValid,
  isWorkspaceSettingsChanged,
  qemuResourceLimits,
  workspaceSettingsForm,
  workspaceUpdatePayload,
} from '@opencuria/chat-core/workspaceCreation'
import type { WorkspaceSelectionSummary } from '@opencuria/chat-core/workspaceConfiguration'
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
/** The visible page: settings, or the pushed "Plugins & Credentials". */
const page = ref<'main' | 'access'>('main')
const selectionSummary = ref<WorkspaceSelectionSummary | null>(null)
const accessRow = ref<HTMLButtonElement | null>(null)
const backButton = ref<InstanceType<typeof Button> | null>(null)
const accessScroller = ref<HTMLElement | null>(null)
let preserveDraftOnClose = false
let resetTimer: ReturnType<typeof setTimeout> | undefined
const diskSizeField = ref<InstanceType<typeof Input> | null>(null)
/** Push or pop the sub page and move focus to where the eye goes. */
async function showPage(next: 'main' | 'access'): Promise<void> {
  if (page.value === next) return
  page.value = next
  if (next === 'access' && accessScroller.value) accessScroller.value.scrollTop = 0
  await nextTick()
  if (next === 'access') (backButton.value?.$el as HTMLElement | undefined)?.focus()
  else accessRow.value?.focus()
}
/** Escape on the sub page goes back instead of closing the dialog. */
function handleEscapeKeyDown(event: KeyboardEvent): void {
  if (page.value !== 'access') return
  event.preventDefault()
  void showPage('main')
}
/** Focus the storage size and keep it in view while plugins and credentials load in. */
function handleOpenAutoFocus(event: Event): void {
  const input = diskSizeField.value?.$el as HTMLInputElement | undefined
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
  // Drafts come from adding a credential, which only the sub page offers.
  page.value = 'access'
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
  page.value = 'main'
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
    !dirty.value ||
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
    page.value = 'main'
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
const form = computed(() => ({
  name: name.value,
  credentialIds: selectedCredentialIds.value,
  pluginIds: selectedPluginIds.value,
  vcpus: qemuVcpus.value,
  memoryMb: qemuMemoryMb.value,
  diskSizeGb: qemuDiskSizeGb.value,
  desktopWidth: desktopWidth.value,
  desktopHeight: desktopHeight.value,
}))
const dirty = computed(() =>
  isWorkspaceSettingsChanged(props.workspace, form.value, qemuLimits.value),
)
const canSave = computed(
  () =>
    !submitting.value &&
    !props.disabled &&
    dirty.value &&
    !!name.value.trim() &&
    pluginSelectionValid.value &&
    resourceSelectionValid.value,
)
/** Memory is stored in MiB and edited in GiB. */
const memoryGb = computed({
  get: () => Math.round((qemuMemoryMb.value / 1024) * 100) / 100,
  set: (value: number) => {
    qemuMemoryMb.value = Math.round(Number(value) * 1024)
  },
})
const qemuValid = computed(() =>
  areQemuResourcesValid(
    { vcpus: qemuVcpus.value, memoryMb: qemuMemoryMb.value, diskSizeGb: qemuDiskSizeGb.value },
    qemuLimits.value,
  ),
)
const desktopValid = computed(() => isDesktopSizeValid(desktopWidth.value, desktopHeight.value))
const limitsHint = computed(() => {
  const limits = qemuLimits.value
  return `Allowed: ${limits.minVcpus}–${limits.maxVcpus} vCPUs, ${limits.minMemoryMb / 1024}–${
    limits.maxMemoryMb / 1024
  } GiB memory, ${limits.minDiskSizeGb}–${limits.maxDiskSizeGb} GiB storage.`
})
/** Resource changes restart a running workspace; say so before saving. */
const restartsOnSave = computed(() => {
  if (props.workspace.status !== WorkspaceStatus.RUNNING) return false
  const current = workspaceSettingsForm(props.workspace, qemuLimits.value)
  return (
    current.vcpus !== qemuVcpus.value ||
    current.memoryMb !== qemuMemoryMb.value ||
    current.diskSizeGb !== qemuDiskSizeGb.value
  )
})

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

    <DialogContent
      class="h-[min(41rem,calc(100dvh-2rem))] gap-0 p-0 sm:max-w-xl"
      data-testid="edit-workspace-dialog"
      @open-auto-focus="handleOpenAutoFocus"
      @escape-key-down="handleEscapeKeyDown"
    >
      <div class="flex shrink-0 items-start gap-1.5 px-6 pt-6 pb-4 pr-14">
        <Transition
          enter-active-class="transition-[opacity,translate] duration-200"
          enter-from-class="opacity-0 translate-x-2"
          leave-active-class="transition-opacity duration-100"
          leave-to-class="opacity-0"
        >
          <Button
            v-if="page === 'access'"
            ref="backButton"
            variant="ghost"
            size="icon-sm"
            type="button"
            class="-mt-1.5 -ml-2.5"
            aria-label="Back to workspace settings"
            data-testid="edit-workspace-back"
            @click="showPage('main')"
          >
            <ChevronLeft />
          </Button>
        </Transition>
        <Transition
          mode="out-in"
          enter-active-class="transition-[opacity,translate] duration-200"
          :enter-from-class="
            page === 'access' ? 'opacity-0 translate-x-3' : 'opacity-0 -translate-x-3'
          "
          leave-active-class="transition-opacity duration-100"
          leave-to-class="opacity-0"
        >
          <DialogHeader :key="page" class="min-w-0">
            <DialogTitle>{{
              page === 'access' ? 'Plugins & Credentials' : 'Edit workspace'
            }}</DialogTitle>
            <DialogDescription>{{
              page === 'access'
                ? 'Plugins add skills and tools. Credentials give agents access to services.'
                : 'Changes apply when you save.'
            }}</DialogDescription>
          </DialogHeader>
        </Transition>
      </div>

      <div class="grid min-h-0 flex-1 grid-cols-1 grid-rows-1 overflow-hidden">
        <div
          class="col-start-1 row-start-1 min-h-0 overflow-y-auto px-6 pt-1 pb-6 transition-[translate,opacity] duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none"
          :class="page === 'main' ? 'translate-x-0 opacity-100' : '-translate-x-1/4 opacity-0'"
          :inert="page !== 'main' || undefined"
          :aria-hidden="page !== 'main' || undefined"
          data-testid="edit-workspace-main"
        >
          <form id="edit-workspace-form" class="flex flex-col gap-6" @submit.prevent="handleSubmit">
            <div>
              <label for="edit-workspace-name" class="mb-1.5 block text-sm font-medium">Name</label>
              <Input
                id="edit-workspace-name"
                v-model="name"
                :disabled="submitting || props.disabled"
                :aria-invalid="!name.trim() || undefined"
                placeholder="Workspace name"
              />
            </div>

            <button
              ref="accessRow"
              type="button"
              class="group flex w-full items-center gap-3 rounded-xl border border-border px-3.5 py-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
              :class="selectionSummary?.issue ? 'border-destructive/40 bg-destructive/5' : ''"
              :disabled="props.disabled"
              data-testid="edit-workspace-access"
              @click="showPage('access')"
            >
              <span
                class="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary"
                aria-hidden="true"
                ><Puzzle :size="17"
              /></span>
              <span class="min-w-0 flex-1">
                <span class="block text-sm font-medium text-foreground">Plugins & Credentials</span>
                <span
                  v-if="selectionSummary?.issue"
                  class="flex items-center gap-1 text-xs text-destructive"
                  ><AlertCircle :size="12" class="shrink-0" aria-hidden="true" />{{
                    selectionSummary.issue
                  }}</span
                >
                <span v-else class="block truncate text-xs text-muted-foreground">{{
                  selectionSummary?.text ?? 'Loading…'
                }}</span>
              </span>
              <ChevronRight
                :size="16"
                class="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5"
                aria-hidden="true"
              />
            </button>

            <section class="space-y-2" aria-labelledby="edit-workspace-resources">
              <h3 id="edit-workspace-resources" class="text-sm font-medium">Resources</h3>
              <div class="divide-y divide-border rounded-xl border border-border">
                <div class="grid grid-cols-[4.5rem_1fr_6.5rem] items-center gap-3 px-3.5 py-2.5">
                  <label for="edit-workspace-vcpus" class="text-sm">CPU</label>
                  <input
                    v-model.number="qemuVcpus"
                    :disabled="submitting || props.disabled"
                    type="range"
                    class="w-full accent-primary"
                    :min="qemuLimits.minVcpus"
                    :max="qemuLimits.maxVcpus"
                    step="1"
                    aria-label="CPU"
                  />
                  <div class="relative">
                    <Input
                      id="edit-workspace-vcpus"
                      v-model.number="qemuVcpus"
                      :disabled="submitting || props.disabled"
                      type="number"
                      class="h-8 pr-12 no-spinner text-right tabular-nums"
                      :min="qemuLimits.minVcpus"
                      :max="qemuLimits.maxVcpus"
                      step="1"
                      :aria-invalid="!qemuValid || undefined"
                    />
                    <span
                      class="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-xs text-muted-foreground"
                      >vCPU</span
                    >
                  </div>
                </div>
                <div class="grid grid-cols-[4.5rem_1fr_6.5rem] items-center gap-3 px-3.5 py-2.5">
                  <label for="edit-workspace-memory" class="text-sm">Memory</label>
                  <input
                    v-model.number="qemuMemoryMb"
                    :disabled="submitting || props.disabled"
                    type="range"
                    class="w-full accent-primary"
                    :min="qemuLimits.minMemoryMb"
                    :max="qemuLimits.maxMemoryMb"
                    step="256"
                    aria-label="Memory"
                  />
                  <div class="relative">
                    <Input
                      id="edit-workspace-memory"
                      v-model.number="memoryGb"
                      :disabled="submitting || props.disabled"
                      type="number"
                      class="h-8 pr-11 no-spinner text-right tabular-nums"
                      :min="qemuLimits.minMemoryMb / 1024"
                      :max="qemuLimits.maxMemoryMb / 1024"
                      step="0.25"
                      :aria-invalid="!qemuValid || undefined"
                    />
                    <span
                      class="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-xs text-muted-foreground"
                      >GiB</span
                    >
                  </div>
                </div>
                <div class="grid grid-cols-[4.5rem_1fr_6.5rem] items-center gap-3 px-3.5 py-2.5">
                  <label for="edit-workspace-disk" class="text-sm">Storage</label>
                  <input
                    v-model.number="qemuDiskSizeGb"
                    :disabled="submitting || props.disabled"
                    type="range"
                    class="w-full accent-primary"
                    :min="qemuLimits.minDiskSizeGb"
                    :max="qemuLimits.maxDiskSizeGb"
                    step="1"
                    aria-label="Storage"
                  />
                  <div class="relative">
                    <Input
                      id="edit-workspace-disk"
                      ref="diskSizeField"
                      v-model.number="qemuDiskSizeGb"
                      :disabled="submitting || props.disabled"
                      type="number"
                      class="h-8 pr-11 no-spinner text-right tabular-nums"
                      :min="qemuLimits.minDiskSizeGb"
                      :max="qemuLimits.maxDiskSizeGb"
                      step="1"
                      :aria-invalid="!qemuValid || undefined"
                    />
                    <span
                      class="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-xs text-muted-foreground"
                      >GiB</span
                    >
                  </div>
                </div>
                <div class="grid grid-cols-[4.5rem_minmax(0,1fr)] items-center gap-3 px-3.5 py-2.5">
                  <span id="edit-workspace-desktop" class="text-sm">Desktop</span>
                  <div
                    class="flex items-center justify-end gap-2"
                    role="group"
                    aria-labelledby="edit-workspace-desktop"
                  >
                    <Input
                      v-model.number="desktopWidth"
                      :disabled="submitting || props.disabled"
                      type="number"
                      class="no-spinner h-8 min-w-0 flex-1 text-right tabular-nums sm:w-24 sm:flex-none"
                      :min="DESKTOP_SIZE_LIMITS.minWidth"
                      :max="DESKTOP_SIZE_LIMITS.maxWidth"
                      step="2"
                      aria-label="Desktop width"
                      :aria-invalid="!desktopValid || undefined"
                    />
                    <span class="text-xs text-muted-foreground" aria-hidden="true">×</span>
                    <Input
                      v-model.number="desktopHeight"
                      :disabled="submitting || props.disabled"
                      type="number"
                      class="no-spinner h-8 min-w-0 flex-1 text-right tabular-nums sm:w-24 sm:flex-none"
                      :min="DESKTOP_SIZE_LIMITS.minHeight"
                      :max="DESKTOP_SIZE_LIMITS.maxHeight"
                      step="2"
                      aria-label="Desktop height"
                      :aria-invalid="!desktopValid || undefined"
                    />
                    <span class="text-xs text-muted-foreground">px</span>
                  </div>
                </div>
              </div>
              <p v-if="!qemuValid" class="text-xs text-destructive" role="alert">
                {{ limitsHint }}
              </p>
              <p v-if="!desktopValid" class="text-xs text-destructive" role="alert">
                Desktop size: even values, {{ DESKTOP_SIZE_LIMITS.minWidth }}–{{
                  DESKTOP_SIZE_LIMITS.maxWidth
                }}
                × {{ DESKTOP_SIZE_LIMITS.minHeight }}–{{ DESKTOP_SIZE_LIMITS.maxHeight }}.
              </p>
              <p class="text-xs text-muted-foreground">
                Changing CPU, memory or storage restarts a running workspace. The desktop size
                applies the next time the desktop starts.
              </p>
            </section>
          </form>
        </div>

        <div
          ref="accessScroller"
          class="col-start-1 row-start-1 min-h-0 overflow-y-auto bg-popover px-6 pt-1 pb-6 transition-[translate] duration-300 ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none"
          :class="page === 'access' ? 'translate-x-0' : 'translate-x-full'"
          :inert="page !== 'access' || undefined"
          :aria-hidden="page !== 'access' || undefined"
          data-testid="edit-workspace-access-page"
        >
          <WorkspacePluginCredentialSelection
            v-model:plugin-ids="selectedPluginIds"
            v-model:credential-ids="selectedCredentialIds"
            :disabled="submitting || props.disabled"
            :active="open"
            @validity-change="pluginSelectionValid = $event"
            @summary-change="selectionSummary = $event"
            @add-credential="navigateToCredentials"
            @reconnect-credential="(id) => navigateToCredentials(undefined, id)"
          />
          <p v-if="draftError" class="mt-4 text-xs text-destructive" role="alert">
            {{ draftError }}
          </p>
        </div>
      </div>

      <DialogFooter class="shrink-0 items-center border-t border-border px-6 py-4">
        <p
          v-if="restartsOnSave"
          class="mr-auto flex items-center gap-1.5 text-xs text-warning"
          data-testid="edit-workspace-restart-hint"
        >
          <RotateCw :size="13" aria-hidden="true" />Saving restarts the workspace.
        </p>
        <Button variant="outline" type="button" :disabled="submitting" @click="handleClose"
          >Cancel</Button
        >
        <Button
          type="submit"
          form="edit-workspace-form"
          data-testid="edit-workspace-save"
          :disabled="!canSave"
        >
          {{ submitting ? 'Saving…' : 'Save changes' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>

<style scoped>
/* The unit sits where the browser's stepper would; sliders and arrow keys step instead. */
.no-spinner {
  appearance: textfield;
}
.no-spinner::-webkit-inner-spin-button,
.no-spinner::-webkit-outer-spin-button {
  appearance: none;
  margin: 0;
}
</style>
