<script setup lang="ts">
import { onMounted, onUnmounted, computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useRunnerStore } from '@/stores/runners'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useConversationStore } from '@/stores/conversations'
import { usePolling } from '@/composables/usePolling'
import {
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
  onEvent,
} from '@/services/socket'
import { SessionStatus, WorkspaceOperation, WorkspaceStatus } from '@/types'
import {
  isConversationIdle,
  isConversationRunning,
  isSessionActive,
} from '@/lib/sessionState'
import { UiInput } from '@/components/ui'
import {
  Search,
  Wifi,
  Container,
  LayoutList,
  LayoutGrid,
} from 'lucide-vue-next'
import CreateWorkspaceDialog from '@/components/workspaces/CreateWorkspaceDialog.vue'
import ConversationListView from '@/components/conversations/ConversationListView.vue'
import ConversationKanbanView from '@/components/conversations/ConversationKanbanView.vue'

const router = useRouter()
const runnerStore = useRunnerStore()
const workspaceStore = useWorkspaceStore()
const conversationStore = useConversationStore()

// Poll runners + workspaces for the stats bar
const { start: startRunnerPolling } = usePolling(() => runnerStore.fetchRunners(), 10000)
const { start: startWorkspacePolling } = usePolling(() => workspaceStore.fetchWorkspaces(), 10000)
// Poll conversations for fresh data
const { start: startConvPolling } = usePolling(() => conversationStore.fetchConversations(), 15000)

// WebSocket cleanup functions
const cleanupFns: (() => void)[] = []
const subscribedWorkspaceIds: string[] = []

function setupSocketListeners(): void {
  for (const wsId of conversationStore.uniqueWorkspaceIds) {
    subscribeToWorkspace(wsId)
    subscribedWorkspaceIds.push(wsId)
  }

  cleanupFns.push(
    onEvent('session:output_chunk', (data) => {
      conversationStore.updateConversationSession(
        data.workspace_id,
        data.chat_id,
        data.session_id,
        SessionStatus.RUNNING,
      )
    }),
  )

  cleanupFns.push(
    onEvent('session:completed', (data) => {
      conversationStore.updateConversationSession(
        data.workspace_id,
        data.chat_id,
        data.session_id,
        SessionStatus.COMPLETED,
      )
    }),
  )

  cleanupFns.push(
    onEvent('session:failed', (data) => {
      conversationStore.updateConversationSession(
        data.workspace_id,
        data.chat_id,
        data.session_id,
        SessionStatus.FAILED,
      )
    }),
  )

  cleanupFns.push(
    onEvent('workspace:status_changed', (data) => {
      conversationStore.updateWorkspaceStatus(data.workspace_id, data.status as WorkspaceStatus)
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
  await conversationStore.fetchConversations()
  startConvPolling()
  setupSocketListeners()
})

onUnmounted(() => {
  cleanupSocket()
})

// View mode — persisted across sessions; default kanban on first visit
const VIEW_MODE_KEY = 'opencuria:dashboard-view'
const savedViewMode = localStorage.getItem(VIEW_MODE_KEY)
const viewMode = ref<'list' | 'kanban'>(
  savedViewMode === 'list' || savedViewMode === 'kanban' ? savedViewMode : 'kanban',
)
watch(viewMode, (v) => localStorage.setItem(VIEW_MODE_KEY, v))

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

// ---------------------------------------------------------------------------
// Kanban columns
// ---------------------------------------------------------------------------

/** Column 1 – Available: no active session, or completed/failed and already read */
const idleConvs = computed(() =>
  conversationStore.filteredConversations.filter((conv) => isConversationIdle(conv)),
)

/** Column 2 – Working: session is pending or running */
const workingConvs = computed(() =>
  conversationStore.filteredConversations.filter((conv) => isConversationRunning(conv)),
)

/** Column 3 – Done/unread: session completed or failed, not yet opened */
const doneConvs = computed(() =>
  conversationStore.filteredConversations.filter((conv) => {
    const status = conv.last_session?.status
    return Boolean(status && !isSessionActive(status) && !conv.is_read)
  }),
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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

function navigateToConversation(conv: {
  workspace_id: string
  chat_id: string | null
  last_session: { id: string } | null
}): void {
  if (conv.last_session) {
    conversationStore.markAsRead(conv.workspace_id, conv.chat_id, conv.last_session.id)
  }
  router.push({
    path: `/workspaces/${conv.workspace_id}`,
    query: conv.chat_id ? { chatId: conv.chat_id } : {},
  })
}
</script>

<template>
  <div class="flex flex-col h-full -m-6 lg:-m-8">
    <!-- Compact stats bar -->
    <div class="border-b border-border bg-surface px-4 py-3 lg:px-6 shrink-0">
      <div class="flex items-center justify-between gap-4">
        <div class="flex items-center gap-4">
          <!-- Runners online -->
          <div class="flex items-center gap-1.5 text-sm">
            <Wifi :size="14" :class="onlineRunnersCount > 0 ? 'text-success' : 'text-muted-fg'" />
            <span class="text-fg font-medium">{{ onlineRunnersCount }}</span>
            <span class="text-muted-fg">/ {{ totalRunnersCount }} runners online</span>
          </div>
          <!-- Active workspaces -->
          <div class="flex items-center gap-1.5 text-sm">
            <Container :size="14" class="text-success" />
            <span class="text-fg font-medium">{{ activeWorkspacesCount }}</span>
            <span class="text-muted-fg">active</span>
          </div>
        </div>
        <div class="hidden sm:block">
          <CreateWorkspaceDialog />
        </div>
      </div>
    </div>

    <!-- Search bar + view toggle -->
    <div class="border-b border-border bg-surface px-4 py-2 lg:px-6 shrink-0">
      <div class="flex items-center gap-2">
        <div class="relative flex-1">
          <Search :size="14" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-fg" />
          <UiInput
            v-model="conversationStore.searchQuery"
            placeholder="Search conversations..."
            class="pl-8 h-8 text-sm"
          />
        </div>
        <!-- View toggle: only on lg+ -->
        <div class="hidden lg:flex items-center gap-0.5 rounded-md border border-border p-0.5">
          <button
            :class="[
              'flex items-center justify-center w-7 h-7 rounded transition-colors',
              viewMode === 'list' ? 'bg-surface-hover text-fg' : 'text-muted-fg hover:text-fg',
            ]"
            title="List view"
            @click="viewMode = 'list'"
          >
            <LayoutList :size="14" />
          </button>
          <button
            :class="[
              'flex items-center justify-center w-7 h-7 rounded transition-colors',
              viewMode === 'kanban' ? 'bg-surface-hover text-fg' : 'text-muted-fg hover:text-fg',
            ]"
            title="Kanban view"
            @click="viewMode = 'kanban'"
          >
            <LayoutGrid :size="14" />
          </button>
        </div>
      </div>
    </div>

    <!-- List view — always visible on mobile; hidden on lg+ when kanban -->
    <div :class="viewMode === 'kanban' ? 'lg:hidden' : ''">
      <ConversationListView
        :conversations="conversationStore.filteredConversations"
        :loading="conversationStore.loading"
        :search-query="conversationStore.searchQuery"
        :format-time-ago="formatTimeAgo"
        :workspace-status-variant="workspaceStatusVariant"
        @conversation-click="navigateToConversation"
      />
    </div>

    <!-- Kanban view — lg+ only, rendered only when viewMode === 'kanban' -->
    <ConversationKanbanView
      v-if="viewMode === 'kanban'"
      :idle-convs="idleConvs"
      :working-convs="workingConvs"
      :done-convs="doneConvs"
      :format-time-ago="formatTimeAgo"
      :workspace-status-variant="workspaceStatusVariant"
      @conversation-click="navigateToConversation"
    />
  </div>
</template>
