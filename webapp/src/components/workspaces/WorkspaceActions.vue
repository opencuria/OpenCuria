<script setup lang="ts">
import { computed } from 'vue'
import { WorkspaceStatus, RuntimeType } from '@/types'
import type { Workspace } from '@/types'
import { Button } from '@/components/ui/button'
import { Square, Play, Trash2, Camera, Loader2, RotateCcw } from '@lucide/vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import EditWorkspaceDialog from './EditWorkspaceDialog.vue'

const props = defineProps<{
  workspace: Workspace
  size?: 'default' | 'sm'
  hideDestructive?: boolean
}>()

const emit = defineEmits<{
  captureImage: []
  recreate: []
}>()

function inspectRunner() {
  window.dispatchEvent(
    new CustomEvent('opencuria:open-settings', {
      detail: { tab: 'runners', runnerId: props.workspace.runner_id },
    }),
  )
}

const workspaceStore = useWorkspaceStore()
const isTransitioning = computed(() => workspaceStore.isWorkspaceTransitioning(props.workspace.id))
const transitionLabel = computed(() =>
  workspaceStore.getWorkspaceTransitionLabel(props.workspace.id),
)
const isRunnerOfflineState = computed(
  () =>
    !props.workspace.runner_online &&
    props.workspace.status !== WorkspaceStatus.DELETED &&
    props.workspace.status !== WorkspaceStatus.REMOVED,
)

const canStop = computed(
  () => !isRunnerOfflineState.value && props.workspace.status === WorkspaceStatus.RUNNING,
)
const canResume = computed(
  () => isRunnerOfflineState.value || props.workspace.status === WorkspaceStatus.STOPPED,
)
const canRemove = computed(
  () =>
    isRunnerOfflineState.value ||
    [WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED, WorkspaceStatus.FAILED].includes(
      props.workspace.status,
    ),
)
const canCaptureImage = computed(
  () =>
    !isRunnerOfflineState.value &&
    props.workspace.runtime_type === RuntimeType.QEMU &&
    (props.workspace.status === WorkspaceStatus.RUNNING ||
      props.workspace.status === WorkspaceStatus.STOPPED),
)
const canRecreate = computed(
  () =>
    !isRunnerOfflineState.value &&
    !!(props.workspace.base_image || props.workspace.pending_base_image) &&
    [WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED, WorkspaceStatus.FAILED].includes(
      props.workspace.status,
    ),
)
const captureBlockedByCredentials = computed(
  () =>
    canCaptureImage.value &&
    props.workspace.status === WorkspaceStatus.STOPPED &&
    props.workspace.credentials_present,
)
const captureTitle = computed(() =>
  captureBlockedByCredentials.value
    ? 'Credentials are still on disk. Stop the workspace to remove them before capturing. If it was stopped externally, resume and stop it again.'
    : 'Capture image',
)
const areActionsDisabled = computed(
  () =>
    isTransitioning.value || isRunnerOfflineState.value || props.workspace.intervention_required,
)

const btnSize = computed(() => (props.size === 'sm' ? ('icon-sm' as const) : ('icon' as const)))

function handleStop(e: Event): void {
  e.stopPropagation()
  workspaceStore.stopWorkspace(props.workspace.id)
}

function handleResume(e: Event): void {
  e.stopPropagation()
  workspaceStore.resumeWorkspace(props.workspace.id)
}

function handleRemove(e: Event): void {
  e.stopPropagation()
  if (confirm('Are you sure you want to remove this workspace? This action cannot be undone.')) {
    workspaceStore.removeWorkspace(props.workspace.id)
  }
}

function handleCaptureImage(e: Event): void {
  e.stopPropagation()
  emit('captureImage')
}

function handleRecreate(e: Event): void {
  e.stopPropagation()
  emit('recreate')
}
</script>

<template>
  <div class="flex flex-wrap items-center gap-1">
    <p v-if="workspace.intervention_required" role="alert" class="text-xs text-destructive">
      {{ workspace.lifecycle_diagnostic || 'Intervention required. Unsafe actions disabled.' }}
    </p>
    <Button
      v-if="workspace.intervention_required"
      variant="outline"
      size="sm"
      @click="inspectRunner"
      >Inspect runner operations (admin)</Button
    >
    <EditWorkspaceDialog :workspace="workspace" :size="size" :disabled="areActionsDisabled" />
    <Button
      v-if="isTransitioning && !workspace.intervention_required"
      variant="ghost"
      :size="btnSize"
      :title="transitionLabel || 'Workspace action in progress'"
      disabled
    >
      <Loader2 :size="14" class="animate-spin" />
    </Button>
    <Button
      v-if="canCaptureImage"
      variant="ghost"
      :size="btnSize"
      :title="captureTitle"
      :disabled="areActionsDisabled || captureBlockedByCredentials"
      @click="handleCaptureImage"
    >
      <Camera :size="14" />
    </Button>
    <Button
      v-if="canRecreate && !hideDestructive"
      variant="ghost"
      :size="btnSize"
      title="Reset workspace"
      data-testid="workspace-actions-recreate"
      :disabled="areActionsDisabled"
      @click="handleRecreate"
    >
      <RotateCcw :size="14" />
    </Button>
    <Button
      v-if="canStop && !hideDestructive"
      variant="ghost"
      :size="btnSize"
      title="Stop workspace"
      :disabled="areActionsDisabled"
      @click="handleStop"
    >
      <Square :size="14" />
    </Button>
    <Button
      v-if="canResume"
      variant="ghost"
      :size="btnSize"
      :title="isRunnerOfflineState ? 'Runner offline' : 'Resume workspace'"
      :disabled="areActionsDisabled"
      @click="handleResume"
    >
      <Play :size="14" />
    </Button>
    <Button
      v-if="canRemove && !hideDestructive"
      variant="ghost"
      :size="btnSize"
      title="Remove workspace"
      class="text-destructive hover:text-destructive"
      :disabled="areActionsDisabled"
      @click="handleRemove"
    >
      <Trash2 :size="14" />
    </Button>
  </div>
</template>
