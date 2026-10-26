<script setup lang="ts">
import { computed, ref } from 'vue'
import { WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import { Button } from '@/components/ui/button'
import {
  Square,
  Play,
  Trash2,
  Camera,
  Loader2,
  RotateCcw,
  MoreHorizontal,
  Pencil,
  AlertTriangle,
} from '@lucide/vue'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useAuthStore } from '@/stores/auth'
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

const editDialog = ref<InstanceType<typeof EditWorkspaceDialog>>()

function inspectRunner() {
  window.dispatchEvent(
    new CustomEvent('opencuria:open-settings', {
      detail: { tab: 'runners', runnerId: props.workspace.runner_id, runnerActivity: true },
    }),
  )
}

const workspaceStore = useWorkspaceStore()
const auth = useAuthStore()
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

function handleEdit(e: Event): void {
  e.stopPropagation()
  editDialog.value?.show()
}

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
  <div class="flex shrink-0 items-center gap-1" @click.stop>
    <Button
      v-if="isTransitioning && !workspace.intervention_required"
      variant="ghost"
      :size="size || 'default'"
      :title="transitionLabel || 'Workspace action in progress'"
      disabled
    >
      <Loader2 :size="14" class="animate-spin" />
      <span class="max-w-24 truncate">{{ transitionLabel || 'Working' }}</span>
    </Button>
    <Button
      v-else-if="canStop && !hideDestructive"
      variant="ghost"
      :size="size || 'default'"
      title="Stop workspace"
      :disabled="areActionsDisabled"
      @click="handleStop"
    >
      <Square :size="14" />
      Stop
    </Button>
    <Button
      v-else-if="canResume"
      variant="secondary"
      :size="size || 'default'"
      :title="isRunnerOfflineState ? 'Runner offline' : 'Resume workspace'"
      :disabled="areActionsDisabled"
      @click="handleResume"
    >
      <Play :size="14" />
      Start
    </Button>
    <DropdownMenu>
      <DropdownMenuTrigger as-child>
        <Button
          variant="ghost"
          :size="size === 'sm' ? 'icon-sm' : 'icon'"
          title="More workspace actions"
          :aria-label="`More actions for ${workspace.name}`"
          class="text-muted-foreground"
        >
          <MoreHorizontal :size="16" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" class="w-56" @click.stop>
        <template v-if="workspace.intervention_required">
          <DropdownMenuLabel
            role="alert"
            class="whitespace-normal text-xs font-normal text-destructive"
          >
            {{
              auth.isAdmin
                ? 'A previous operation could not be confirmed. Review its status to continue.'
                : 'A previous operation could not be confirmed. Ask an organization admin to review it.'
            }}
          </DropdownMenuLabel>
          <DropdownMenuItem v-if="auth.isAdmin" @select="inspectRunner">
            <AlertTriangle />
            Review workspace issue
          </DropdownMenuItem>
          <DropdownMenuSeparator />
        </template>
        <DropdownMenuItem :disabled="areActionsDisabled" @select="handleEdit">
          <Pencil />
          Edit workspace
        </DropdownMenuItem>
        <DropdownMenuItem
          v-if="canCaptureImage"
          :title="captureTitle"
          :disabled="areActionsDisabled || captureBlockedByCredentials"
          @select="handleCaptureImage"
        >
          <Camera />
          Capture image
        </DropdownMenuItem>
        <DropdownMenuItem
          v-if="canRecreate && !hideDestructive"
          data-testid="workspace-actions-recreate"
          :disabled="areActionsDisabled"
          @select="handleRecreate"
        >
          <RotateCcw />
          Reset workspace
        </DropdownMenuItem>
        <template v-if="canRemove && !hideDestructive">
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            :disabled="areActionsDisabled"
            @select="handleRemove"
          >
            <Trash2 />
            Remove workspace
          </DropdownMenuItem>
        </template>
      </DropdownMenuContent>
    </DropdownMenu>
    <EditWorkspaceDialog
      ref="editDialog"
      :workspace="workspace"
      :disabled="areActionsDisabled"
      hide-trigger
    />
  </div>
</template>
