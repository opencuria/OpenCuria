<script setup lang="ts">
import { computed, ref } from 'vue'
import { WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import { UiCard, UiCardContent, UiBadge } from '@/components/ui'
import { AlertTriangle, Container, Clock, Loader2, WifiOff } from 'lucide-vue-next'
import { formatRelativeTime } from '@/lib/utils'
import { useWorkspaceStore } from '@/stores/workspaces'
import WorkspaceActions from './WorkspaceActions.vue'
import WorkspaceImageArtifactDialog from './WorkspaceImageArtifactDialog.vue'

const props = defineProps<{
  workspace: Workspace
  showResourceWarning?: boolean
  clickable?: boolean
}>()

defineEmits<{
  click: []
}>()

const imageArtifactDialogOpen = ref(false)
const workspaceStore = useWorkspaceStore()

function handleCaptureImage(): void {
  imageArtifactDialogOpen.value = true
}

const transitionLabel = computed(
  () => workspaceStore.getWorkspaceTransitionLabel(props.workspace.id),
)
const isRunnerOfflineState = computed(
  () => !props.workspace.runner_online && props.workspace.status !== WorkspaceStatus.REMOVED,
)

const statusVariant = computed(() => {
  if (transitionLabel.value) {
    return 'info'
  }
  if (isRunnerOfflineState.value) {
    return 'muted'
  }
  switch (props.workspace.status) {
    case WorkspaceStatus.RUNNING:
      return 'success'
    case WorkspaceStatus.CREATING:
      return 'warning'
    case WorkspaceStatus.STOPPED:
      return 'muted'
    case WorkspaceStatus.FAILED:
      return 'error'
    case WorkspaceStatus.REMOVED:
      return 'muted'
    default:
      return 'muted'
  }
})

const statusLabel = computed(() => {
  if (isRunnerOfflineState.value) {
    return 'Runner offline'
  }
  return transitionLabel.value ?? props.workspace.status
})
const showStatusSpinner = computed(
  () => Boolean(transitionLabel.value) && !isRunnerOfflineState.value,
)
const imminentAutoStopLabel = computed(() => {
  if (!props.workspace.auto_stop_at || props.workspace.status !== WorkspaceStatus.RUNNING) {
    return null
  }
  const remainingMs = new Date(props.workspace.auto_stop_at).getTime() - Date.now()
  if (remainingMs <= 0 || remainingMs > 10 * 60 * 1000) {
    return null
  }
  return `Stops ${formatRelativeTime(props.workspace.auto_stop_at)}`
})
</script>

<template>
  <UiCard
    :class="'transition-colors duration-150' + (clickable ? ' cursor-pointer hover:border-border-hover' : '')"
    @click="clickable ? $emit('click') : undefined"
  >
    <UiCardContent>
      <div class="flex items-start justify-between gap-3 mb-3">
        <div class="flex items-center gap-3 min-w-0 flex-1">
          <div
            :class="[
              'flex items-center justify-center w-10 h-10 rounded-[var(--radius-md)]',
               isRunnerOfflineState
                 ? 'bg-muted text-muted-fg'
                 : workspace.status === WorkspaceStatus.RUNNING
                  ? 'bg-success-muted text-success'
                  : workspace.status === WorkspaceStatus.CREATING
                    ? 'bg-warning-muted text-warning'
                    : workspace.status === WorkspaceStatus.FAILED
                      ? 'bg-error-muted text-error'
                      : workspace.status === WorkspaceStatus.REMOVED
                        ? 'bg-muted/50 text-muted-fg/50'
                        : 'bg-muted text-muted-fg',
            ]"
          >
            <Container :size="18" />
          </div>
          <div class="min-w-0">
            <h3 class="font-medium text-fg text-sm truncate">
              {{ workspace.name }}
            </h3>
            <div class="text-xs text-muted-fg font-mono">
              {{ workspace.id.slice(0, 8) }}…
            </div>
          </div>
        </div>

        <div class="flex items-center gap-1.5 shrink-0">
          <UiBadge
            v-if="!isRunnerOfflineState && workspace.status === WorkspaceStatus.RUNNING && workspace.has_active_session"
            variant="warning"
            class="flex items-center gap-1"
          >
            <Loader2 :size="10" class="animate-spin" />
            Busy
          </UiBadge>
          <UiBadge
            v-if="showResourceWarning"
            variant="warning"
            class="flex items-center gap-1"
          >
            <AlertTriangle :size="10" />
            High usage
          </UiBadge>
          <UiBadge :variant="statusVariant" class="flex items-center gap-1">
            <WifiOff v-if="isRunnerOfflineState" :size="10" />
            <Loader2 v-else-if="showStatusSpinner" :size="10" class="animate-spin" />
            {{ statusLabel }}
          </UiBadge>
        </div>
      </div>

      <!-- Footer: Time + Actions -->
      <div class="flex items-center justify-between gap-3">
        <div class="min-w-0 text-xs text-muted-fg">
          <div class="flex items-center gap-1.5">
            <Clock :size="12" />
            {{ formatRelativeTime(workspace.created_at) }}
          </div>
          <div v-if="imminentAutoStopLabel" class="mt-1 text-warning">
            {{ imminentAutoStopLabel }}
          </div>
        </div>
        <WorkspaceActions :workspace="workspace" size="sm" @capture-image="handleCaptureImage" />
      </div>
    </UiCardContent>
  </UiCard>

  <WorkspaceImageArtifactDialog
    v-if="imageArtifactDialogOpen"
    :workspace="workspace"
    :open="imageArtifactDialogOpen"
    @update:open="imageArtifactDialogOpen = $event"
  />
</template>
