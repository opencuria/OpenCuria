<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useHarnessStore } from '@/stores/harness'
import { useProcessesStore } from '@/stores/processes'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { usePolling } from '@/composables/usePolling'
import {
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
  onEvent,
} from '@/services/socket'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import { formatRelativeTime } from '@/lib/utils'
import HarnessChatPanel from '@/components/chat/HarnessChatPanel.vue'
import WorkspaceChatHeader from '@/components/chat/WorkspaceChatHeader.vue'
import WorkspaceImageArtifactDialog from '@/components/workspaces/WorkspaceImageArtifactDialog.vue'
import WorkspaceToolsSplit from '@/components/workspaces/WorkspaceToolsSplit.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Button } from '@/components/ui/button'
import { useSidePanelStore } from '@/stores/sidePanel'

const route = useRoute()
const router = useRouter()
const workspaceStore = useWorkspaceStore()
const processesStore = useProcessesStore()
const sidePanelStore = useSidePanelStore()

const workspaceId = computed(() => route.params.id as string)
const workspace = computed(() => workspaceStore.activeWorkspace)
const workspaceImageStore = useWorkspaceImageStore()
const renamingWorkspace = ref(false)
const processesOpen = ref(false)
const imageArtifactDialogOpen = ref(false)

const canPrompt = computed(
  () =>
    workspace.value?.status === WorkspaceStatus.RUNNING &&
    workspace.value?.runner_online &&
    !workspace.value?.active_operation,
)
const isRunnerOfflineState = computed(
  () =>
    !workspace.value?.runner_online &&
    workspace.value?.status !== WorkspaceStatus.DELETED &&
    workspace.value?.status !== WorkspaceStatus.REMOVED,
)
const workspaceTransitionLabel = computed(() =>
  workspaceStore.getWorkspaceTransitionLabel(workspaceId.value),
)
const autoStopCountdownLabel = computed(() =>
  workspace.value?.auto_stop_at ? `Stops ${formatRelativeTime(workspace.value.auto_stop_at)}` : null,
)
const navbarStatusLabel = computed(() => {
  if (!showImminentAutoStop.value || !autoStopCountdownLabel.value) return null
  return autoStopCountdownLabel.value
})
const showImminentAutoStop = computed(() => {
  if (!workspace.value?.auto_stop_at || workspace.value.status !== WorkspaceStatus.RUNNING) {
    return false
  }
  const remainingMs = new Date(workspace.value.auto_stop_at).getTime() - Date.now()
  return remainingMs > 0 && remainingMs <= 10 * 60 * 1000
})

const harnessStore = useHarnessStore()

const activeChatTitle = computed(
  () => harnessStore.activeSession?.title?.trim() || null,
)

function handleNewHarnessChat(): void {
  harnessStore.setActiveSession(null)
}

function handleDeleteWorkspace(): void {
  if (!workspace.value) return
  if (confirm('Are you sure you want to remove this workspace? This action cannot be undone.')) {
    void workspaceStore.removeWorkspace(workspace.value.id)
  }
}

function handleStartWorkspace(): void {
  if (!workspace.value) return
  void workspaceStore.resumeWorkspace(workspace.value.id)
}

function handleStopWorkspace(): void {
  if (!workspace.value) return
  void workspaceStore.stopWorkspace(workspace.value.id)
}

const runningProcessCount = computed(() =>
  processesStore.runningCountFor(workspaceId.value),
)
const isProcessesPanelVisible = computed(
  () => processesOpen.value && canPrompt.value,
)

function toggleProcessesPanel(): void {
  processesOpen.value = !processesOpen.value
  if (processesOpen.value) {
    void processesStore.fetchProcesses(workspaceId.value)
  }
}

// Socket.IO event cleanup functions
const cleanupFns: (() => void)[] = []

function setupSocketListeners(): void {
  subscribeToWorkspace(workspaceId.value)

  cleanupFns.push(
    onEvent('workspace:status_changed', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.updateWorkspaceStatus(
          data.workspace_id,
          data.status as WorkspaceStatus,
          data.credentials_present,
        )
      }
    }),
  )

  cleanupFns.push(
    onEvent('workspace:operation_changed', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.updateWorkspaceOperation(
          data.workspace_id,
          data.active_operation as WorkspaceOperation | null,
        )
      }
    }),
  )

  cleanupFns.push(
    onEvent('workspace:error', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.handleWorkspaceError(data.workspace_id, data.error)
      }
    }),
  )

  cleanupFns.push(
    onEvent('runner:offline', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.updateWorkspaceRunnerOnline(data.workspace_id, false)
      }
    }),
  )

  cleanupFns.push(
    onEvent('runner:online', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.updateWorkspaceRunnerOnline(data.workspace_id, true)
      }
    }),
  )

  cleanupFns.push(
    onEvent('process:status_changed', (data) => {
      if (data.workspace_id === workspaceId.value) {
        void processesStore.handleStatusChanged(data)
      }
    }),
  )

  cleanupFns.push(
    onEvent('process:removed', (data) => {
      if (data.workspace_id === workspaceId.value) {
        processesStore.handleRemoved(data)
      }
    }),
  )
}

function cleanupSocket(): void {
  unsubscribeFromWorkspace(workspaceId.value)
  cleanupFns.forEach((fn) => fn())
  cleanupFns.length = 0
}

// Polling for workspace detail (fallback + initial load)
const { start, stop } = usePolling(
  () => workspaceStore.fetchWorkspaceDetail(workspaceId.value),
  5000,
)

// Polling fallback for background processes (socket is the live path)
const {
  start: startProcessesPolling,
  stop: stopProcessesPolling,
} = usePolling(() => processesStore.fetchProcesses(workspaceId.value), 10000)

onMounted(() => {
  start()
  startProcessesPolling()
  setupSocketListeners()
})

onUnmounted(() => {
  stop()
  stopProcessesPolling()
  cleanupSocket()
  processesStore.reset()
  workspaceImageStore.reset()
  harnessStore.reset()
  workspaceStore.activeWorkspace = null
})

watch(workspaceId, (newId, oldId) => {
  if (newId !== oldId) {
    cleanupSocket()
    processesStore.clearWorkspace(oldId)
    processesOpen.value = false
    harnessStore.reset()
    workspaceStore.fetchWorkspaceDetail(newId)
    void processesStore.fetchProcesses(newId)
    setupSocketListeners()
  }
})

function goBack(): void {
  if (window.history.state?.back) {
    router.back()
  } else {
    router.push('/workspaces')
  }
}

async function handleSaveWorkspaceName(name: string): Promise<void> {
  if (!workspace.value || workspaceTransitionLabel.value) return

  const trimmed = name.trim()
  if (!trimmed || trimmed === workspace.value.name) return

  renamingWorkspace.value = true
  await workspaceStore.renameWorkspace(workspace.value.id, trimmed)
  renamingWorkspace.value = false
}
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- Loading state -->
    <div v-if="workspaceStore.loading && !workspace" class="flex-1 flex items-center justify-center">
      <LoadingSpinner :size="24" />
    </div>

    <WorkspaceToolsSplit
      v-else-if="workspace"
      :workspace-id="workspaceId"
      :can-prompt="canPrompt"
    >
      <template #header>
        <WorkspaceChatHeader
          :workspace="workspace"
          :active-chat-title="activeChatTitle"
          :transition-label="workspaceTransitionLabel"
          :auto-stop-label="navbarStatusLabel"
          :runner-offline="isRunnerOfflineState"
          :side-panel-open="sidePanelStore.isOpen"
          :processes-active="isProcessesPanelVisible"
          :running-process-count="runningProcessCount"
          :can-prompt="canPrompt"
          @new-chat="handleNewHarnessChat"
          @start-workspace="handleStartWorkspace"
          @stop-workspace="handleStopWorkspace"
          @save-workspace-name="handleSaveWorkspaceName"
          @toggle-side-panel="sidePanelStore.toggle()"
          @toggle-processes="toggleProcessesPanel"
          @capture-image="imageArtifactDialogOpen = true"
          @delete-workspace="handleDeleteWorkspace"
        />
      </template>

      <HarnessChatPanel
        :workspace-id="workspaceId"
        :can-prompt="canPrompt"
        :processes-open="isProcessesPanelVisible"
        class="min-h-0 flex-1"
        @close-processes="processesOpen = false"
      />
    </WorkspaceToolsSplit>

    <!-- Error -->
    <div
      v-else-if="workspaceStore.error"
      class="flex-1 flex items-center justify-center"
    >
      <div class="text-center">
        <p class="text-error mb-2">{{ workspaceStore.error }}</p>
        <Button variant="outline" @click="goBack">
          Back
        </Button>
      </div>
    </div>
  </div>

  <WorkspaceImageArtifactDialog
    v-if="workspace && imageArtifactDialogOpen"
    :workspace="workspace"
    :open="imageArtifactDialogOpen"
    @update:open="imageArtifactDialogOpen = $event"
  />
</template>
