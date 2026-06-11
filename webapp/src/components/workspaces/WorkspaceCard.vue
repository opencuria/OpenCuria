<script setup lang="ts">
import { computed, ref } from 'vue'
import { WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { AlertTriangle, Container, Loader2, Layers, WifiOff } from '@lucide/vue'
import { formatRelativeTime } from '@/lib/utils'
import { useWorkspaceStore } from '@/stores/workspaces'
import WorkspaceActions from './WorkspaceActions.vue'
import WorkspaceImageArtifactDialog from './WorkspaceImageArtifactDialog.vue'

const props = defineProps<{
  workspace: Workspace
  storageBytes?: number | null
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
  () =>
    !props.workspace.runner_online &&
    props.workspace.status !== WorkspaceStatus.DELETED &&
    props.workspace.status !== WorkspaceStatus.REMOVED,
)

const statusVariant = computed(() => {
  if (transitionLabel.value) {
    return 'default'
  }
  if (isRunnerOfflineState.value) {
    return 'secondary'
  }
  switch (props.workspace.status) {
    case WorkspaceStatus.RUNNING:
      return 'secondary'
    case WorkspaceStatus.CREATING:
      return 'outline'
    case WorkspaceStatus.STOPPED:
      return 'secondary'
    case WorkspaceStatus.FAILED:
    case WorkspaceStatus.DELETE_FAILED:
      return 'destructive'
    case WorkspaceStatus.REMOVED:
    case WorkspaceStatus.DELETED:
      return 'secondary'
    default:
      return 'secondary'
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

function formatStorage(bytes?: number | null): string {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}
</script>

<template>
  <Card
    :class="'transition-colors duration-150' + (clickable ? ' cursor-pointer hover:border-border' : '')"
    @click="clickable ? $emit('click') : undefined"
  >
    <CardContent>
      <div class="flex items-start justify-between gap-3 mb-3">
        <div class="flex items-center gap-3 min-w-0 flex-1">
          <div
            :class="[
              'flex items-center justify-center w-10 h-10 rounded-[var(--radius-md)]',
               isRunnerOfflineState
                 ? 'bg-muted text-muted-foreground'
                 : workspace.status === WorkspaceStatus.RUNNING
                  ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                      : workspace.status === WorkspaceStatus.CREATING
                    ? 'bg-amber-500/10 text-amber-600 dark:text-amber-400'
                    : workspace.status === WorkspaceStatus.FAILED || workspace.status === WorkspaceStatus.DELETE_FAILED
                      ? 'bg-destructive/10 text-destructive'
                      : workspace.status === WorkspaceStatus.REMOVED || workspace.status === WorkspaceStatus.DELETED
                         ? 'bg-muted/50 text-muted-foreground/50'
                         : 'bg-muted text-muted-foreground',
            ]"
          >
            <Container :size="18" />
          </div>
          <div class="min-w-0">
            <h3 class="font-medium text-foreground text-sm truncate">
              {{ workspace.name }}
            </h3>
            <div
              v-if="workspace.base_image_name"
              class="mt-1 inline-flex max-w-full items-center gap-1 rounded-[var(--radius-sm)] bg-muted/60 px-1.5 py-0.5 text-[11px] text-muted-foreground"
              :title="`Based on image: ${workspace.base_image_name}`"
            >
              <Layers :size="11" class="shrink-0" />
              <span class="truncate">{{ workspace.base_image_name }}</span>
            </div>
            <div v-else class="mt-1 text-xs text-muted-foreground">—</div>
          </div>
        </div>

        <div class="flex items-center gap-1.5 shrink-0">
          <Badge
            v-if="!isRunnerOfflineState && workspace.status === WorkspaceStatus.RUNNING && workspace.has_active_session"
            variant="outline"
            class="flex items-center gap-1"
          >
            <Loader2 :size="10" class="animate-spin" />
            Busy
          </Badge>
          <Badge
            v-if="showResourceWarning"
            variant="outline"
            class="flex items-center gap-1"
          >
            <AlertTriangle :size="10" />
            High usage
          </Badge>
          <Badge :variant="statusVariant" class="flex items-center gap-1">
            <WifiOff v-if="isRunnerOfflineState" :size="10" />
            <Loader2 v-else-if="showStatusSpinner" :size="10" class="animate-spin" />
            {{ statusLabel }}
          </Badge>
        </div>
      </div>

      <!-- Footer: Storage + Actions -->
      <div class="flex items-center justify-between gap-3">
        <div class="min-w-0 text-xs text-muted-foreground font-mono">
          <div>{{ formatStorage(storageBytes) }}</div>
          <div v-if="imminentAutoStopLabel" class="mt-1 text-amber-600 dark:text-amber-400">
            {{ imminentAutoStopLabel }}
          </div>
        </div>
        <WorkspaceActions :workspace="workspace" size="sm" @capture-image="handleCaptureImage" />
      </div>
    </CardContent>
  </Card>

  <WorkspaceImageArtifactDialog
    v-if="imageArtifactDialogOpen"
    :workspace="workspace"
    :open="imageArtifactDialogOpen"
    @update:open="imageArtifactDialogOpen = $event"
  />
</template>
