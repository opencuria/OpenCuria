<script setup lang="ts">
import { computed, ref } from 'vue'
import { WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { AlertTriangle, Container, Loader2, WifiOff } from '@lucide/vue'
import { formatRelativeTime } from '@/lib/utils'
import { USAGE_LEVEL_CLASSES, describeDiskUsage, isDiskUsageStale } from '@/lib/workspaceDisk'
import { useWorkspaceStore } from '@/stores/workspaces'
import WorkspaceActions from './WorkspaceActions.vue'
import WorkspaceImageArtifactDialog from './WorkspaceImageArtifactDialog.vue'
import WorkspaceRecreateDialog from './WorkspaceRecreateDialog.vue'
import ImageVersionBadge from '@/components/images/ImageVersionBadge.vue'
import UsageMeter from '@/components/common/UsageMeter.vue'
import { storageBytes } from '@/composables/useRunnerStorage'

const props = withDefaults(
  defineProps<{
    workspace: Workspace
    showResourceWarning?: boolean
    clickable?: boolean
  }>(),
  { clickable: true },
)

defineEmits<{
  click: []
}>()

const imageArtifactDialogOpen = ref(false)
const recreateDialogOpen = ref(false)
const recreatePreferLatest = ref(false)

function openRecreate(preferLatest: boolean): void {
  recreatePreferLatest.value = preferLatest
  recreateDialogOpen.value = true
}
const workspaceStore = useWorkspaceStore()

function handleCaptureImage(): void {
  imageArtifactDialogOpen.value = true
}

const transitionLabel = computed(() =>
  workspaceStore.getWorkspaceTransitionLabel(props.workspace.id),
)
const isRunnerOfflineState = computed(
  () =>
    !props.workspace.runner_online &&
    props.workspace.status !== WorkspaceStatus.DELETED &&
    props.workspace.status !== WorkspaceStatus.REMOVED,
)

const statusVariant = computed(() => {
  if (props.workspace.intervention_required) return 'destructive'
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
  if (props.workspace.intervention_required) return 'Needs review'
  if (transitionLabel.value) return transitionLabel.value
  if (isRunnerOfflineState.value) {
    return 'Runner offline'
  }
  if (props.workspace.status === WorkspaceStatus.RUNNING && props.workspace.has_active_session) {
    return 'Busy'
  }
  return props.workspace.status.replace(/_/g, ' ')
})
const showStatusSpinner = computed(
  () =>
    !isRunnerOfflineState.value &&
    !props.workspace.intervention_required &&
    (Boolean(transitionLabel.value) ||
      (props.workspace.status === WorkspaceStatus.RUNNING && props.workspace.has_active_session)),
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

const disk = computed(() => {
  const usage = props.workspace.disk_usage
  if (
    !usage ||
    props.workspace.status === WorkspaceStatus.DELETED ||
    props.workspace.status === WorkspaceStatus.REMOVED
  ) {
    return null
  }
  const view = describeDiskUsage(usage)
  const measured =
    props.workspace.status !== WorkspaceStatus.RUNNING || isDiskUsageStale(usage)
      ? ` · measured ${formatRelativeTime(usage.observed_at)}`
      : ''
  return {
    ...view,
    free: storageBytes(view.freeBytes),
    title: `${storageBytes(view.usedBytes)} of ${storageBytes(view.capacityBytes)} used (${view.percent}%)${measured}`,
  }
})
</script>

<template>
  <Card
    :class="[
      'h-full min-w-0 gap-0 py-5 transition-[background-color,box-shadow] duration-150',
      props.clickable && 'cursor-pointer hover:bg-accent/20 hover:ring-foreground/20',
    ]"
    @click="props.clickable ? $emit('click') : undefined"
  >
    <CardContent class="grid grid-rows-[4rem_2.5rem_2.75rem] gap-2 px-5">
      <div class="flex min-w-0 items-start gap-3">
        <div class="flex min-w-0 flex-1 items-start gap-3">
          <div
            :class="[
              'flex size-10 shrink-0 items-center justify-center rounded-xl',
              isRunnerOfflineState
                ? 'bg-muted text-muted-foreground'
                : workspace.status === WorkspaceStatus.RUNNING
                  ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                  : workspace.status === WorkspaceStatus.CREATING
                    ? 'bg-amber-500/10 text-amber-600 dark:text-amber-400'
                    : workspace.status === WorkspaceStatus.FAILED ||
                        workspace.status === WorkspaceStatus.DELETE_FAILED
                      ? 'bg-destructive/10 text-destructive'
                      : workspace.status === WorkspaceStatus.REMOVED ||
                          workspace.status === WorkspaceStatus.DELETED
                        ? 'bg-muted/50 text-muted-foreground/50'
                        : 'bg-muted text-muted-foreground',
            ]"
          >
            <Container :size="18" />
          </div>
          <div class="min-w-0 flex-1">
            <h3
              class="truncate text-sm font-semibold leading-5 text-foreground"
              :title="workspace.name"
            >
              {{ workspace.name }}
            </h3>
            <ImageVersionBadge :workspace="workspace" @update="openRecreate(true)" />
          </div>
        </div>

        <span
          v-if="showResourceWarning"
          class="shrink-0 text-amber-600 dark:text-amber-400"
          title="High usage"
        >
          <AlertTriangle :size="16" />
          <span class="sr-only">High usage</span>
        </span>
      </div>

      <!-- Storage: last observed free space inside the workspace -->
      <div class="flex min-w-0 flex-col justify-center gap-1.5">
        <div
          v-if="disk"
          class="flex items-center gap-3 text-xs"
          :title="disk.title"
          data-testid="workspace-card-disk"
        >
          <UsageMeter
            :percent="disk.percent"
            :level="disk.level"
            label="Storage used"
            class="h-1 flex-1"
          />
          <span class="shrink-0 tabular-nums" :class="USAGE_LEVEL_CLASSES[disk.level].text"
            >{{ disk.free }} free</span
          >
        </div>
        <span v-else class="text-xs text-muted-foreground/60">Storage not measured</span>
        <span
          v-if="imminentAutoStopLabel"
          class="truncate text-xs text-amber-600 dark:text-amber-400"
          :title="imminentAutoStopLabel"
        >
          {{ imminentAutoStopLabel }}
        </span>
      </div>

      <div class="flex min-w-0 items-center justify-between gap-2 border-t border-border/60 pt-3">
        <Badge
          :variant="statusVariant"
          class="min-w-0 shrink gap-1.5 capitalize"
          :class="
            !isRunnerOfflineState &&
            !workspace.intervention_required &&
            workspace.status === WorkspaceStatus.RUNNING &&
            !transitionLabel
              ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
              : ''
          "
          :title="statusLabel"
        >
          <AlertTriangle v-if="workspace.intervention_required" :size="10" />
          <WifiOff v-else-if="isRunnerOfflineState" :size="10" />
          <Loader2 v-else-if="showStatusSpinner" :size="10" class="animate-spin" />
          <span
            v-else-if="workspace.status === WorkspaceStatus.RUNNING"
            class="size-1.5 shrink-0 rounded-full bg-current"
          />
          <span class="truncate">{{ statusLabel }}</span>
        </Badge>
        <WorkspaceActions
          :workspace="workspace"
          size="sm"
          @capture-image="handleCaptureImage"
          @recreate="openRecreate(false)"
        />
      </div>
    </CardContent>
  </Card>

  <WorkspaceImageArtifactDialog
    v-if="imageArtifactDialogOpen"
    :workspace="workspace"
    :open="imageArtifactDialogOpen"
    @update:open="imageArtifactDialogOpen = $event"
  />
  <WorkspaceRecreateDialog
    v-if="recreateDialogOpen"
    :workspace="workspace"
    :open="recreateDialogOpen"
    :prefer-latest="recreatePreferLatest"
    @update:open="recreateDialogOpen = $event"
  />
</template>
