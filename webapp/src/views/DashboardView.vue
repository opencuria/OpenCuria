<script setup lang="ts">
import { onMounted, computed, ref } from 'vue'
import { useRunnerStore } from '@/stores/runners'
import { useWorkspaceStore } from '@/stores/workspaces'
import { usePolling } from '@/composables/usePolling'
import {
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
  onEvent,
} from '@/services/socket'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import { Input } from '@/components/ui/input'
import {
  Search,
  Wifi,
  Container,
} from '@lucide/vue'
import { useRouter } from 'vue-router'
import CreateWorkspaceDialog from '@/components/workspaces/CreateWorkspaceDialog.vue'
import WorkspaceList from '@/components/workspaces/WorkspaceList.vue'
import type { Workspace } from '@/types'

const router = useRouter()
const runnerStore = useRunnerStore()
const workspaceStore = useWorkspaceStore()
const searchQuery = ref('')

// Poll runners + workspaces
const { start: startRunnerPolling } = usePolling(() => runnerStore.fetchRunners(), 10000)
const { start: startWorkspacePolling } = usePolling(() => workspaceStore.fetchWorkspaces(), 10000)

// WebSocket cleanup functions
const cleanupFns: (() => void)[] = []
const subscribedWorkspaceIds: string[] = []

function setupSocketListeners(): void {
  for (const workspace of workspaceStore.workspaces) {
    subscribeToWorkspace(workspace.id)
    subscribedWorkspaceIds.push(workspace.id)
  }

  cleanupFns.push(
    onEvent('workspace:status_changed', (data) => {
      workspaceStore.updateWorkspaceStatus(data.workspace_id, data.status as WorkspaceStatus)
    }),
  )

  cleanupFns.push(
    onEvent('workspace:operation_changed', (data) => {
      workspaceStore.updateWorkspaceOperation(
        data.workspace_id,
        data.active_operation as WorkspaceOperation | null,
      )
    }),
  )

  cleanupFns.push(
    onEvent('workspace:error', (data) => {
      workspaceStore.handleWorkspaceError(data.workspace_id, data.error)
    }),
  )

  cleanupFns.push(
    onEvent('runner:offline', (data) => {
      workspaceStore.updateWorkspaceRunnerOnline(data.workspace_id, false)
    }),
  )

  cleanupFns.push(
    onEvent('runner:online', (data) => {
      workspaceStore.updateWorkspaceRunnerOnline(data.workspace_id, true)
    }),
  )
}

function cleanupSocket(): void {
  for (const wsId of subscribedWorkspaceIds) {
    unsubscribeFromWorkspace(wsId)
  }
  subscribedWorkspaceIds.length = 0
  cleanupFns.forEach((fn) => fn())
  cleanupFns.length = 0
}

onMounted(async () => {
  startRunnerPolling()
  startWorkspacePolling()
  await workspaceStore.fetchWorkspaces()
  setupSocketListeners()
})

import { onUnmounted } from 'vue'

onUnmounted(() => {
  cleanupSocket()
})

// Stats for the compact header bar
const onlineRunnersCount = computed(() => runnerStore.onlineRunners.length)
const totalRunnersCount = computed(() => runnerStore.runners.length)
const activeWorkspacesCount = computed(
  () =>
    workspaceStore.workspaces.filter(
      (workspace) =>
        workspace.status === WorkspaceStatus.RUNNING && workspace.runner_online,
    ).length,
)

function workspaceStatusVariant(
  status: WorkspaceStatus,
): 'success' | 'warning' | 'error' | 'muted' {
  switch (status) {
    case WorkspaceStatus.RUNNING:
      return 'success'
    case WorkspaceStatus.CREATING:
      return 'warning'
    case WorkspaceStatus.FAILED:
      return 'error'
    default:
      return 'muted'
  }
}

const filteredWorkspaces = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  const list = workspaceStore.workspaces.filter(
    (w) => w.status !== WorkspaceStatus.DELETED && w.status !== WorkspaceStatus.REMOVED,
  )
  if (!q) return list
  return list.filter(
    (w) => w.name.toLowerCase().includes(q) || w.id.toLowerCase().includes(q),
  )
})

function openWorkspace(workspace: Workspace): void {
  router.push({ name: 'workspace-detail', params: { id: workspace.id } })
}

function formatTimeAgo(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime()
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}
</script>

<template>
  <div class="flex flex-col h-full -m-6 lg:-m-8">
    <!-- Compact stats bar -->
    <div class="border-b border-border bg-header px-4 py-3 lg:px-6 shrink-0">
      <div class="flex items-center justify-between gap-4">
        <div class="flex items-center gap-4">
          <!-- Runners online -->
          <div class="flex items-center gap-1.5 text-sm">
            <Wifi :size="14" :class="onlineRunnersCount > 0 ? 'text-success' : 'text-muted-foreground'" />
            <span class="text-foreground font-medium">{{ onlineRunnersCount }}</span>
            <span class="text-muted-foreground">/ {{ totalRunnersCount }} runners online</span>
          </div>
          <!-- Active workspaces -->
          <div class="flex items-center gap-1.5 text-sm">
            <Container :size="14" class="text-success" />
            <span class="text-foreground font-medium">{{ activeWorkspacesCount }}</span>
            <span class="text-muted-foreground">active</span>
          </div>
        </div>
        <div class="hidden sm:block">
          <CreateWorkspaceDialog />
        </div>
      </div>
    </div>

    <!-- Search bar -->
    <div class="border-b border-border bg-header px-4 py-2 lg:px-6 shrink-0">
      <div class="flex items-center gap-2">
        <div class="relative flex-1">
          <Search :size="14" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
          <Input
            v-model="searchQuery"
            placeholder="Search workspaces..."
            class="pl-8 h-8 text-sm"
          />
        </div>
      </div>
    </div>

    <div class="flex-1 overflow-y-auto px-4 py-4 lg:px-6">
      <WorkspaceList :workspaces="filteredWorkspaces" @select="openWorkspace" />
    </div>
  </div>
</template>

