<script setup lang="ts">
/**
 * ChatSidebar — chat-first navigation: brand, new chat, command palette,
 * active sessions, time-grouped conversations, compact workspaces, account.
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Layers, Plus, Search } from '@lucide/vue'
import CommandPalette from './CommandPalette.vue'
import ActiveConversationsSection from './sidebar/ActiveConversationsSection.vue'
import ConversationTimeList from './sidebar/ConversationTimeList.vue'
import SidebarBrandHeader from './sidebar/SidebarBrandHeader.vue'
import SidebarUserFooter from './sidebar/SidebarUserFooter.vue'
import WorkspaceSection from './sidebar/WorkspaceSection.vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenuButton,
  SidebarRail,
  useSidebar,
} from '@/components/ui/sidebar'
import { usePolling } from '@/composables/usePolling'
import {
  countableWorkspaces,
  conversationTitle,
  extractActiveConversations,
  selectSidebarWorkspaces,
} from '@/lib/conversationGroups'
import { useAuthStore } from '@/stores/auth'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { useHarnessStore } from '@/stores/harness'
import { useWorkspaceStore } from '@/stores/workspaces'
import {
  connect as connectSocket,
  disconnect as disconnectSocket,
  onEvent,
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
} from '@/services/socket'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { HarnessConversation } from '@/types/harness'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const workspaceStore = useWorkspaceStore()
const conversationStore = useHarnessConversationStore()
const harnessStore = useHarnessStore()
const { isMobile, setOpenMobile } = useSidebar()

const searchOpen = ref(false)
const deleteTarget = ref<HarnessConversation | null>(null)

const activeConversations = computed(() =>
  extractActiveConversations(conversationStore.conversations),
)

const timeListConversations = computed(() => {
  const activeIds = new Set(activeConversations.value.map((row) => row.session_id))
  return conversationStore.conversations.filter((row) => !activeIds.has(row.session_id))
})

const sidebarWorkspaces = computed(() =>
  selectSidebarWorkspaces(workspaceStore.workspaces, conversationStore.conversations),
)

const workspaceTotal = computed(
  () => countableWorkspaces(workspaceStore.workspaces).length,
)

const activeSessionId = computed(() => {
  const query = route.query.session
  const value = Array.isArray(query) ? query[0] : query
  return typeof value === 'string' ? value : null
})

const activeWorkspaceId = computed(() => {
  const id = route.params.id
  return typeof id === 'string' ? id : null
})

function closeMobileSidebar(): void {
  if (isMobile.value) setOpenMobile(false)
}

function handleNewChat(): void {
  closeMobileSidebar()
  void router.push('/')
}

function handleSelectConversation(conversation: HarnessConversation): void {
  void conversationStore.markAsRead(conversation.session_id)
  closeMobileSidebar()
  void router.push({
    path: `/workspaces/${conversation.workspace_id}`,
    query: { session: conversation.session_id },
  })
}

function handleOpenWorkspace(workspaceId: string): void {
  closeMobileSidebar()
  void router.push({ path: `/workspaces/${workspaceId}` })
}

function handleOpenWorkspaces(): void {
  closeMobileSidebar()
  void router.push('/workspaces')
}

function handleMarkAllRead(): void {
  const unread = conversationStore.conversations.filter((row) => row.unread)
  void Promise.all(unread.map((row) => conversationStore.markAsRead(row.session_id)))
}

async function handleRename(conversation: HarnessConversation, title: string): Promise<void> {
  await harnessStore.renameSession(conversation.session_id, title)
  await conversationStore.fetchConversations()
}

function handleMarkRead(conversation: HarnessConversation): void {
  void conversationStore.markAsRead(conversation.session_id)
}

function requestDelete(conversation: HarnessConversation): void {
  deleteTarget.value = conversation
}

async function confirmDelete(): Promise<void> {
  if (!deleteTarget.value) return
  const sessionId = deleteTarget.value.session_id
  deleteTarget.value = null
  await harnessStore.removeSession(sessionId)
  await conversationStore.fetchConversations()
}

function switchOrganization(orgId: string): void {
  authStore.setActiveOrganization(orgId)
  disconnectSocket()
  connectSocket()
  router.go(0)
}

function handleLogout(): void {
  authStore.logout()
  disconnectSocket()
  void router.push('/login')
}

function handleOpenSettings(): void {
  closeMobileSidebar()
  window.dispatchEvent(new CustomEvent('opencuria:open-settings'))
}

const { start: startWorkspacePolling } = usePolling(() => workspaceStore.fetchWorkspaces(), 10000)
const { start: startConvPolling } = usePolling(() => conversationStore.fetchConversations(), 15000)

const cleanupFns: (() => void)[] = []
const subscribedWorkspaceIds: string[] = []

function subscribeVisibleWorkspaces(): void {
  const ids = new Set<string>([
    ...workspaceStore.workspaces.map((workspace) => workspace.id),
    ...conversationStore.uniqueWorkspaceIds,
  ])
  for (const workspaceId of ids) {
    if (subscribedWorkspaceIds.includes(workspaceId)) continue
    subscribeToWorkspace(workspaceId)
    subscribedWorkspaceIds.push(workspaceId)
  }
}

function setupSocketListeners(): void {
  subscribeVisibleWorkspaces()

  cleanupFns.push(
    onEvent('harness.session_status', (data) => {
      conversationStore.updateSessionStatus(
        data.session_id,
        data.status,
        harnessStore.viewingSessionId === data.session_id,
      )
      harnessStore.handleSessionStatus(data.session_id, data.status, {
        model: data.model,
        reasoning_effort: data.reasoning_effort,
      })
    }),
  )

  cleanupFns.push(
    onEvent('harness.part_updated', (data) => {
      conversationStore.touchConversation(data.session_id)
    }),
  )

  cleanupFns.push(
    onEvent('workspace:status_changed', (data) => {
      workspaceStore.updateWorkspaceStatus(
        data.workspace_id,
        data.status as WorkspaceStatus,
        data.credentials_present,
      )
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
  for (const workspaceId of subscribedWorkspaceIds) {
    unsubscribeFromWorkspace(workspaceId)
  }
  subscribedWorkspaceIds.length = 0
  cleanupFns.forEach((fn) => fn())
  cleanupFns.length = 0
}

function handleGlobalKeydown(event: KeyboardEvent): void {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault()
    searchOpen.value = true
  }
}

onMounted(async () => {
  window.addEventListener('keydown', handleGlobalKeydown)
  startWorkspacePolling()
  await workspaceStore.fetchWorkspaces()
  await conversationStore.fetchConversations()
  startConvPolling()
  setupSocketListeners()
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleGlobalKeydown)
  cleanupSocket()
})

watch(
  () => conversationStore.uniqueWorkspaceIds,
  () => {
    subscribeVisibleWorkspaces()
  },
)
</script>

<template>
  <Sidebar collapsible="icon">
    <SidebarHeader>
      <SidebarBrandHeader
        @home="closeMobileSidebar"
        @switch-organization="switchOrganization"
      />

      <div class="px-2 pt-1 group-data-[collapsible=icon]:hidden">
        <Button class="w-full rounded-xl" size="sm" @click="handleNewChat">
          <Plus class="size-4" />
          Neuer Chat
        </Button>
        <button
          type="button"
          class="mt-1.5 flex w-full items-center gap-2 rounded-xl px-2 py-1.5 text-left text-[13px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary"
          @click="searchOpen = true"
        >
          <Search class="size-4 shrink-0" />
          <span class="flex-1 truncate">Suchen</span>
          <kbd class="rounded border border-border bg-background px-1 text-[10px]">⌘K</kbd>
        </button>
      </div>

      <div class="hidden flex-col items-center gap-1 pt-1 group-data-[collapsible=icon]:flex">
        <SidebarMenuButton tooltip="Neuer Chat" @click="handleNewChat">
          <Plus />
        </SidebarMenuButton>
        <SidebarMenuButton tooltip="Suchen (⌘K)" @click="searchOpen = true">
          <Search />
        </SidebarMenuButton>
        <SidebarMenuButton tooltip="Workspaces" @click="handleOpenWorkspaces">
          <Layers />
        </SidebarMenuButton>
      </div>
    </SidebarHeader>

    <SidebarContent class="group-data-[collapsible=icon]:hidden">
      <div class="flex flex-col gap-3 pb-2">
        <ActiveConversationsSection
          :conversations="activeConversations"
          :active-session-id="activeSessionId"
          @select="handleSelectConversation"
          @rename="handleRename"
          @delete="requestDelete"
          @mark-read="handleMarkRead"
          @mark-all-read="handleMarkAllRead"
        />

        <ConversationTimeList
          v-if="conversationStore.conversations.length === 0 || timeListConversations.length > 0"
          :conversations="timeListConversations"
          :active-session-id="activeSessionId"
          :empty="conversationStore.conversations.length === 0"
          @select="handleSelectConversation"
          @rename="handleRename"
          @delete="requestDelete"
          @mark-read="handleMarkRead"
        />

        <WorkspaceSection
          :workspaces="sidebarWorkspaces"
          :total-count="workspaceTotal"
          :active-workspace-id="activeWorkspaceId"
          @open="handleOpenWorkspace"
          @open-all="handleOpenWorkspaces"
          @create="handleOpenWorkspaces"
        />
      </div>
    </SidebarContent>

    <SidebarFooter>
      <SidebarUserFooter
        @settings="handleOpenSettings"
        @logout="handleLogout"
        @navigate="closeMobileSidebar"
      />
    </SidebarFooter>

    <SidebarRail />
  </Sidebar>

  <CommandPalette v-model:open="searchOpen" />

  <Dialog :open="deleteTarget !== null" @update:open="(open) => { if (!open) deleteTarget = null }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Chat löschen?</DialogTitle>
        <DialogDescription>
          „{{ deleteTarget ? conversationTitle(deleteTarget) : '' }}“ wird dauerhaft gelöscht.
          Ein laufender Lauf wird abgebrochen.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">Abbrechen</Button>
        <Button variant="destructive" @click="confirmDelete">Löschen</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
