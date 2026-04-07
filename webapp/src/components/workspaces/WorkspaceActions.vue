<script setup lang="ts">
import { computed } from 'vue'
import { WorkspaceStatus, RuntimeType } from '@/types'
import type { Workspace } from '@/types'
import { UiButton } from '@/components/ui'
import { Square, Play, Trash2, Camera, Loader2 } from 'lucide-vue-next'
import { useWorkspaceStore } from '@/stores/workspaces'
import EditWorkspaceDialog from './EditWorkspaceDialog.vue'

const props = defineProps<{
  workspace: Workspace
  size?: 'default' | 'sm'
  hideDestructive?: boolean
}>()

const emit = defineEmits<{
  captureImage: []
}>()

const workspaceStore = useWorkspaceStore()
const isTransitioning = computed(() => workspaceStore.isWorkspaceTransitioning(props.workspace.id))
const transitionLabel = computed(() => workspaceStore.getWorkspaceTransitionLabel(props.workspace.id))
const isRunnerOfflineState = computed(
  () => !props.workspace.runner_online && props.workspace.status !== WorkspaceStatus.REMOVED,
)

const canStop = computed(
  () => !isRunnerOfflineState.value && props.workspace.status === WorkspaceStatus.RUNNING,
)
const canResume = computed(
  () => isRunnerOfflineState.value || props.workspace.status === WorkspaceStatus.STOPPED,
)
const canRemove = computed(() =>
  isRunnerOfflineState.value ||
  [WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED, WorkspaceStatus.FAILED].includes(
    props.workspace.status,
  ),
)
const canCaptureImage = computed(
  () =>
    !isRunnerOfflineState.value &&
    props.workspace.runtime_type === RuntimeType.QEMU &&
    props.workspace.status === WorkspaceStatus.RUNNING,
)
const areActionsDisabled = computed(() => isTransitioning.value || isRunnerOfflineState.value)

const btnSize = computed(() => (props.size === 'sm' ? 'icon-sm' as const : 'icon' as const))

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
</script>

<template>
  <div class="flex items-center gap-1">
    <EditWorkspaceDialog :workspace="workspace" :size="size" :disabled="areActionsDisabled" />
    <UiButton
      v-if="isTransitioning"
      variant="ghost"
      :size="btnSize"
      :title="transitionLabel || 'Workspace action in progress'"
      disabled
    >
      <Loader2 :size="14" class="animate-spin" />
    </UiButton>
    <UiButton
      v-if="canCaptureImage"
      variant="ghost"
      :size="btnSize"
      title="Capture image"
      :disabled="areActionsDisabled"
      @click="handleCaptureImage"
    >
      <Camera :size="14" />
    </UiButton>
    <UiButton
      v-if="canStop && !hideDestructive"
      variant="ghost"
      :size="btnSize"
      title="Stop workspace"
      :disabled="areActionsDisabled"
      @click="handleStop"
    >
      <Square :size="14" />
    </UiButton>
    <UiButton
      v-if="canResume"
      variant="ghost"
      :size="btnSize"
      :title="isRunnerOfflineState ? 'Runner offline' : 'Resume workspace'"
      :disabled="areActionsDisabled"
      @click="handleResume"
    >
      <Play :size="14" />
    </UiButton>
    <UiButton
      v-if="canRemove && !hideDestructive"
      variant="ghost"
      :size="btnSize"
      title="Remove workspace"
      class="text-error hover:text-error"
      :disabled="areActionsDisabled"
      @click="handleRemove"
    >
      <Trash2 :size="14" />
    </UiButton>
  </div>
</template>
