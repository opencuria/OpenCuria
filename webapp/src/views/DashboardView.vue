<script setup lang="ts">
import { onMounted, onUnmounted, computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useRunnerStore } from '@/stores/runners'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { usePolling } from '@/composables/usePolling'
import {
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
  onEvent,
} from '@/services/socket'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { HarnessConversation } from '@/types/harness'
import {
  isHarnessConversationAvailable,
  isHarnessConversationDoneUnread,
  isHarnessConversationRunning,
} from '@/lib/harnessConversationState'
import { Input } from '@/components/ui/input'
import {
  Search,
  Wifi,
  Container,
  LayoutList,
  LayoutGrid,
} from '@lucide/vue'
import CreateWorkspaceDialog from '@/components/workspaces/CreateWorkspaceDialog.vue'
import HarnessConversationListView from '@/components/conversations/HarnessConversationListView.vue'
import HarnessConversationKanbanView from '@/components/conversations/HarnessConversationKanbanView.vue'

const router = useRouter()
const runnerStore = useRunnerStore()
const workspaceStore = useWorkspaceStore()
const conversationStore = useHarnessConversationStore()

const { start: startRunnerPolling } = usePolling(() => runnerStore.fetchRunners(), 10000)
const { start: startWorkspacePolling } = usePolling(() => workspaceStore.fetchWorkspaces(), 10000)
const { start: startConvPolling } = usePolling(() => conversationStore.fetchConversations(), 15000)

const cleanupFns: (() => void)[] = []
const subscribedWorkspaceIds: string[] = []

function subscribeConversationWorkspaces(): void {
  for (const wsId of conversationStore.uniqueWorkspaceIds) {
    if (subscribedWorkspaceIds.includes(wsId)) continue
    subscribeToWorkspace(wsId)
    subscribedWorkspaceIds.push(wsId)
  }
}

function setupSocketListeners(): void {
  subscribeConversationWorkspaces()

  cleanupFns.push(
    onEvent('harness.session_status', (data) => {
      conversationStore.updateSessionStatus(data.session_id, data.status)
    }),
  )

  cleanupFns.push(
    onEvent('harness.part_updated', (data) => {
      conversationStore.touchConversation(data.session_id)
    }),
  )

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
  await conversationStore.fetchConversations()
  startConvPolling()
  setupSocketListeners()
})

onUnmounted(() => {
  cleanupSocket()
})

watch(
  () => conversationStore.uniqueWorkspaceIds,
  () => {
    subscribeConversationWorkspaces()
  },
)

const VIEW_MODE_KEY = 'opencuria:dashboard-view'
const savedViewMode = localStorage.getItem(VIEW_MODE_KEY)
const viewMode = ref<'list' | 'kanban'>(
  savedViewMode === 'list' || savedViewMode === 'kanban' ? savedViewMode : 'kanban',
)
watch(viewMode, (value) => localStorage.setItem(VIEW_MODE_KEY, value))

const onlineRunnersCount = computed(() => runnerStore.onlineRunners.length)
const totalRunnersCount = computed(() => runnerStore.runners.length)
const activeWorkspacesCount = computed(
  () =>
    workspaceStore.workspaces.filter(
      (workspace) =>
        workspace.status === WorkspaceStatus.RUNNING && workspace.runner_online,
    ).length,
)

const idleConvs = computed(() =>
  conversationStore.filteredConversations.filter((conv) => isHarnessConversationAvailable(conv)),
)

const workingConvs = computed(() =>
  conversationStore.filteredConversations.filter((conv) => isHarnessConversationRunning(conv)),
)

const doneConvs = computed(() =>
  conversationStore.filteredConversations.filter((conv) => isHarnessConversationDoneUnread(conv)),
)

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

function navigateToConversation(conv: HarnessConversation): void {
  void conversationStore.markAsRead(conv.session_id)
  void router.push({
    path: `/workspaces/${conv.workspace_id}`,
    query: { session: conv.session_id },
  })
}
</script>

<template>
  <div class="flex flex-col h-full -m-6 lg:-m-8">
    <div class="border-b border-border bg-header px-4 py-3 lg:px-6 shrink-0">
      <div class="flex items-center justify-between gap-4">
        <div class="flex items-center gap-4">
          <div class="flex items-center gap-1.5 text-sm">
            <Wifi
              :size="14"
              :class="onlineRunnersCount > 0 ? 'text-success' : 'text-muted-foreground'"
            />
            <span class="text-foreground font-medium">{{ onlineRunnersCount }}</span>
            <span class="text-muted-foreground">/ {{ totalRunnersCount }} runners online</span>
          </div>
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

    <div class="border-b border-border bg-header px-4 py-2 lg:px-6 shrink-0">
      <div class="flex items-center gap-2">
        <div class="relative flex-1">
          <Search
            :size="14"
            class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            v-model="conversationStore.searchQuery"
            placeholder="Search conversations..."
            class="pl-8 h-8 text-sm"
          />
        </div>
        <div class="hidden lg:flex items-center gap-0.5 rounded-md border border-border p-0.5">
          <button
            type="button"
            :class="[
              'flex items-center justify-center w-7 h-7 rounded transition-colors',
              viewMode === 'list' ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground',
            ]"
            title="List view"
            @click="viewMode = 'list'"
          >
            <LayoutList :size="14" />
          </button>
          <button
            type="button"
            :class="[
              'flex items-center justify-center w-7 h-7 rounded transition-colors',
              viewMode === 'kanban' ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground',
            ]"
            title="Kanban view"
            @click="viewMode = 'kanban'"
          >
            <LayoutGrid :size="14" />
          </button>
        </div>
      </div>
    </div>

    <div :class="viewMode === 'kanban' ? 'lg:hidden flex flex-col flex-1 min-h-0' : 'flex flex-col flex-1 min-h-0'">
      <HarnessConversationListView
        :conversations="conversationStore.filteredConversations"
        :loading="conversationStore.loading"
        :search-query="conversationStore.searchQuery"
        :format-time-ago="formatTimeAgo"
        @conversation-click="navigateToConversation"
      />
    </div>

    <HarnessConversationKanbanView
      v-if="viewMode === 'kanban'"
      :idle-convs="idleConvs"
      :working-convs="workingConvs"
      :done-convs="doneConvs"
      :format-time-ago="formatTimeAgo"
      @conversation-click="navigateToConversation"
    />
  </div>
</template>
