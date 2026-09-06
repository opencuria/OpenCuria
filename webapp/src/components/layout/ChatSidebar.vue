<script setup lang="ts">
/**
 * ChatSidebar — ChatGPT-style navigation shell (Schritt 1 Redesign).
 *
 * Ersetzt die alte AppSidebar-Navigation (Dashboard/Settings/…).
 * Gruppierung NUR nach Workspace; Sortierung updated_at desc.
 *
 * Subtask-Darstellung: HarnessConversation (org-weit, Ticket-Feed) kennt keine
 * parent_id — Child-Sessions kommen aus dem Harness-Store
 * (`childSessionsByParent`, key = parent session id == conversation session_id).
 * Der Harness-Store ist nur für den aktuell geöffneten Workspace geladen,
 * daher erscheinen eingerückte Subtasks primär dort; der Fallback ist
 * bewusst dokumentiert statt stillschweigend weggelassen.
 */

import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  BookOpen,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronsUpDown,
  CornerDownRight,
  FolderOpen,
  LogOut,
  MessageSquare,
  Monitor,
  Moon,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Settings,
  Sun,
  Trash2,
  Wifi,
  WifiOff,
  X,
} from '@lucide/vue'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import SearchModal from './SearchModal.vue'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  useSidebar,
} from '@/components/ui/sidebar'
import { useTheme } from '@/composables/useTheme'
import { usePolling } from '@/composables/usePolling'
import { useAuthStore } from '@/stores/auth'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { useHarnessStore } from '@/stores/harness'
import { useWorkspaceStore } from '@/stores/workspaces'
import {
  connect as connectSocket,
  disconnect as disconnectSocket,
  isConnected,
  onEvent,
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
} from '@/services/socket'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { HarnessConversation } from '@/types/harness'
import type { HarnessSession } from '@/types/harness'

const GROUP_KEY = 'opencuria:chat-sidebar'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const workspaceStore = useWorkspaceStore()
const conversationStore = useHarnessConversationStore()
const harnessStore = useHarnessStore()
const { mode, setTheme } = useTheme()
const { isMobile, setOpenMobile } = useSidebar()

const searchOpen = ref(false)
const editingSessionId = ref<string | null>(null)
const editTitle = ref('')
const deleteTarget = ref<HarnessConversation | null>(null)

// --- Collapsible Gruppen-State (Default: alle offen) ------------------------

function loadGroupState(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(GROUP_KEY) ?? '{}')
  } catch {
    return {}
  }
}

const groupState = ref<Record<string, boolean>>(loadGroupState())

function isGroupOpen(id: string): boolean {
  return groupState.value[id] !== false
}

function toggleGroup(id: string, open: boolean): void {
  groupState.value[id] = open
  localStorage.setItem(GROUP_KEY, JSON.stringify(groupState.value))
}

// --- Daten ------------------------------------------------------------------

interface WorkspaceGroup {
  id: string
  name: string
  running: boolean
  conversations: HarnessConversation[]
  unreadCount: number
}

const workspaceGroups = computed<WorkspaceGroup[]>(() =>
  workspaceStore.workspaces.map((ws) => {
    const conversations = conversationStore.conversations
      .filter((conv) => conv.workspace_id === ws.id)
      .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
    return {
      id: ws.id,
      name: ws.name,
      running: ws.status === WorkspaceStatus.RUNNING && ws.runner_online,
      conversations,
      unreadCount: conversations.filter((conv) => conv.unread).length,
    }
  }),
)

function childrenOf(sessionId: string): HarnessSession[] {
  return harnessStore.childSessionsByParent[sessionId] ?? []
}

function statusDotClass(group: WorkspaceGroup): string {
  return group.running ? 'bg-green-500' : 'bg-muted-foreground/40'
}

function formatTimeAgo(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return 'now'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d`
  const weeks = Math.floor(days / 7)
  if (weeks < 52) return `${weeks}w`
  return `${Math.floor(days / 365)}y`
}

function conversationTitle(conv: HarnessConversation): string {
  return conv.title?.trim() || 'New chat'
}

function isActiveConversation(conv: HarnessConversation): boolean {
  const q = route.query.session
  const activeSessionId = Array.isArray(q) ? q[0] : q
  return route.params.id === conv.workspace_id && activeSessionId === conv.session_id
}

function isActiveChild(child: HarnessSession): boolean {
  const q = route.query.session
  const activeSessionId = Array.isArray(q) ? q[0] : q
  return activeSessionId === child.id
}

function closeMobileSidebar(): void {
  if (isMobile.value) setOpenMobile(false)
}

function handleNewChat(): void {
  closeMobileSidebar()
  void router.push('/')
}

function handleSelectConversation(conv: HarnessConversation): void {
  void conversationStore.markAsRead(conv.session_id)
  closeMobileSidebar()
  void router.push({
    path: `/workspaces/${conv.workspace_id}`,
    query: { session: conv.session_id },
  })
}

function handleSelectChild(child: HarnessSession, workspaceId: string): void {
  void conversationStore.markAsRead(child.id)
  closeMobileSidebar()
  void router.push({
    path: `/workspaces/${workspaceId}`,
    query: { session: child.id },
  })
}

function handleEmptyGroupClick(workspaceId: string): void {
  closeMobileSidebar()
  void router.push({ path: `/workspaces/${workspaceId}` })
}

function handleOpenWorkspace(workspaceId: string): void {
  closeMobileSidebar()
  void router.push({ path: `/workspaces/${workspaceId}` })
}

function handleMarkAllRead(group: WorkspaceGroup): void {
  const unread = group.conversations.filter((conv) => conv.unread)
  void Promise.all(unread.map((conv) => conversationStore.markAsRead(conv.session_id)))
}

// --- Inline Rename / Delete (wie HarnessChatSidebar) ------------------------

function startRename(conv: HarnessConversation): void {
  editingSessionId.value = conv.session_id
  editTitle.value = conversationTitle(conv)
}

function cancelRename(): void {
  editingSessionId.value = null
  editTitle.value = ''
}

async function confirmRename(): Promise<void> {
  if (!editingSessionId.value || !editTitle.value.trim()) return
  const sessionId = editingSessionId.value
  const title = editTitle.value.trim()
  editingSessionId.value = null
  editTitle.value = ''
  await harnessStore.renameSession(sessionId, title)
  await conversationStore.fetchConversations()
}

function requestDelete(conv: HarnessConversation): void {
  deleteTarget.value = conv
}

async function confirmDelete(): Promise<void> {
  if (!deleteTarget.value) return
  const sessionId = deleteTarget.value.session_id
  deleteTarget.value = null
  await harnessStore.removeSession(sessionId)
  await conversationStore.fetchConversations()
}

// --- Org-Switcher / User-Menü ------------------------------------------------

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

const userInitials = computed(() => {
  const email = authStore.user?.email ?? ''
  return email.charAt(0).toUpperCase() || '?'
})

// --- Live-Updates + Polling ---------------------------------------------------

const { start: startWorkspacePolling } = usePolling(() => workspaceStore.fetchWorkspaces(), 10000)
const { start: startConvPolling } = usePolling(() => conversationStore.fetchConversations(), 15000)

const cleanupFns: (() => void)[] = []
const subscribedWorkspaceIds: string[] = []

function subscribeVisibleWorkspaces(): void {
  const ids = new Set<string>([
    ...workspaceStore.workspaces.map((ws) => ws.id),
    ...conversationStore.uniqueWorkspaceIds,
  ])
  for (const wsId of ids) {
    if (subscribedWorkspaceIds.includes(wsId)) continue
    subscribeToWorkspace(wsId)
    subscribedWorkspaceIds.push(wsId)
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
  for (const wsId of subscribedWorkspaceIds) {
    unsubscribeFromWorkspace(wsId)
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
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton size="lg" as-child tooltip="OpenCuria">
            <RouterLink to="/" @click="closeMobileSidebar">
              <OpenCuriaLogo icon-only alt="OpenCuria" class="size-8" />
              <span class="font-semibold">OpenCuria</span>
            </RouterLink>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>

      <SidebarMenu v-if="authStore.organizations.length > 0" class="group-data-[collapsible=icon]:hidden">
        <SidebarMenuItem>
          <DropdownMenu>
            <DropdownMenuTrigger as-child>
              <SidebarMenuButton class="h-8 text-xs">
                <Avatar class="size-5 rounded-md">
                  <AvatarFallback class="rounded-md text-[10px]">
                    {{ authStore.activeOrganization?.name?.charAt(0)?.toUpperCase() ?? '?' }}
                  </AvatarFallback>
                </Avatar>
                <span class="truncate">
                  {{ authStore.activeOrganization?.name ?? 'Select organization' }}
                </span>
                <ChevronsUpDown class="ml-auto size-4 shrink-0" />
              </SidebarMenuButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent class="min-w-56" align="start">
              <DropdownMenuItem
                v-for="org in authStore.organizations"
                :key="org.id"
                @click="switchOrganization(org.id)"
              >
                <span class="truncate">{{ org.name }}</span>
                <Check v-if="org.id === authStore.activeOrganizationId" class="ml-auto size-4" />
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem as-child>
                <RouterLink to="/create-organization" class="flex items-center gap-2">
                  <Plus class="size-4" />
                  New organization
                </RouterLink>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </SidebarMenuItem>
      </SidebarMenu>

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
      </div>
    </SidebarHeader>

    <SidebarContent class="group-data-[collapsible=icon]:hidden">
      <div class="flex flex-col gap-1 px-2">
        <Collapsible
          v-for="group in workspaceGroups"
          :key="group.id"
          :open="isGroupOpen(group.id)"
          class="group/collapsible"
          @update:open="(open) => toggleGroup(group.id, open)"
        >
          <div class="flex items-center gap-1.5">
            <CollapsibleTrigger
              class="flex h-6 min-w-0 flex-1 items-center gap-1.5 rounded-md text-xs text-muted-foreground focus-visible:outline-2 focus-visible:outline-primary"
              :aria-expanded="isGroupOpen(group.id)"
            >
              <span class="size-1.5 shrink-0 rounded-full" :class="statusDotClass(group)" />
              <span class="min-w-0 flex-1 truncate text-left font-medium">{{ group.name }}</span>
              <span class="shrink-0 text-[11px] tabular-nums">{{ group.conversations.length }}</span>
              <span
                v-if="group.unreadCount > 0"
                class="size-1.5 shrink-0 rounded-full bg-primary"
                data-testid="group-unread-dot"
              />
              <ChevronDown class="size-3.5 shrink-0 transition-transform group-data-[state=open]/collapsible:rotate-180" />
            </CollapsibleTrigger>
            <DropdownMenu>
              <DropdownMenuTrigger as-child>
                <button
                  type="button"
                  class="flex size-6 shrink-0 items-center justify-center rounded-md text-muted-foreground opacity-0 transition-opacity hover:bg-muted hover:text-foreground focus-visible:opacity-100 focus-visible:outline-2 focus-visible:outline-primary group-hover:opacity-100"
                  :aria-label="`Workspace-Menü für ${group.name}`"
                  @click.stop
                >
                  <MoreHorizontal class="size-3.5" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" class="w-48">
                <DropdownMenuItem @click="handleOpenWorkspace(group.id)">
                  <FolderOpen class="size-4" />
                  Workspace öffnen
                </DropdownMenuItem>
                <DropdownMenuItem :disabled="group.unreadCount === 0" @click="handleMarkAllRead(group)">
                  <CheckCheck class="size-4" />
                  Alle als gelesen
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          <CollapsibleContent>
            <div v-if="group.conversations.length > 0" class="flex flex-col gap-0.5 py-1">
              <template v-for="conv in group.conversations" :key="conv.session_id">
                <div
                  role="button"
                  tabindex="0"
                  :aria-selected="isActiveConversation(conv)"
                  :aria-label="`Chat ${conversationTitle(conv)} öffnen`"
                  class="group/row flex cursor-pointer items-center gap-2 rounded-xl px-2 py-1.5 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-primary"
                  :class="isActiveConversation(conv) ? 'bg-primary/10' : 'hover:bg-muted'"
                  @click="handleSelectConversation(conv)"
                  @keydown.enter="handleSelectConversation(conv)"
                >
                  <template v-if="editingSessionId === conv.session_id">
                    <Input
                      v-model="editTitle"
                      class="h-7 flex-1 text-xs"
                      maxlength="255"
                      aria-label="Chat umbenennen"
                      @keydown.enter.prevent="confirmRename"
                      @keydown.esc.prevent="cancelRename"
                      @click.stop
                    />
                    <Button variant="ghost" size="icon-sm" class="h-6 w-6 shrink-0" aria-label="Umbenennen bestätigen" @click.stop="confirmRename">
                      <Check class="size-3" />
                    </Button>
                    <Button variant="ghost" size="icon-sm" class="h-6 w-6 shrink-0" aria-label="Umbenennen abbrechen" @click.stop="cancelRename">
                      <X class="size-3" />
                    </Button>
                  </template>
                  <template v-else>
                    <div class="min-w-0 flex-1">
                      <div class="truncate text-[13px] font-medium text-foreground">
                        {{ conversationTitle(conv) }}
                      </div>
                      <div class="truncate text-[11px] text-muted-foreground">
                        {{ formatTimeAgo(conv.updated_at) }}
                      </div>
                    </div>
                    <span
                      v-if="conv.unread"
                      class="size-1.5 shrink-0 rounded-full bg-primary"
                      data-testid="unread-dot"
                    />
                    <div class="flex shrink-0 opacity-0 transition-opacity group-hover/row:opacity-100 focus-within:opacity-100">
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        class="h-6 w-6"
                        aria-label="Chat umbenennen"
                        @click.stop="startRename(conv)"
                      >
                        <Pencil class="size-3" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        class="h-6 w-6 text-destructive hover:text-destructive"
                        aria-label="Chat löschen"
                        @click.stop="requestDelete(conv)"
                      >
                        <Trash2 class="size-3" />
                      </Button>
                    </div>
                  </template>
                </div>

                <button
                  v-for="child in childrenOf(conv.session_id)"
                  :key="child.id"
                  type="button"
                  :aria-selected="isActiveChild(child)"
                  :aria-label="`Subtask ${child.title || 'New chat'} öffnen`"
                  class="group/childrow ml-5 flex cursor-pointer items-center gap-1.5 rounded-xl px-2 py-1 text-left text-sm transition-colors focus-visible:outline-2 focus-visible:outline-primary"
                  :class="isActiveChild(child) ? 'bg-primary/10' : 'hover:bg-muted'"
                  @click="handleSelectChild(child, group.id)"
                >
                  <CornerDownRight class="size-3 shrink-0 text-muted-foreground" />
                  <span class="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                    {{ child.title?.trim() || 'New chat' }}
                  </span>
                </button>
              </template>
            </div>
            <button
              v-else
              type="button"
              class="flex w-full items-center gap-2 rounded-xl px-2 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary"
              @click="handleEmptyGroupClick(group.id)"
            >
              <MessageSquare class="size-3.5 shrink-0" />
              <span class="truncate">Keine Chats — Enter zum Starten</span>
            </button>
          </CollapsibleContent>
        </Collapsible>

        <div
          v-if="workspaceGroups.length === 0"
          class="flex flex-col items-center gap-2 py-8 text-muted-foreground"
        >
          <MessageSquare class="size-6 opacity-40" />
          <span class="text-xs">Keine Workspaces</span>
        </div>
      </div>
    </SidebarContent>

    <SidebarFooter>
      <div class="px-2 pb-1 group-data-[collapsible=icon]:hidden">
        <Badge :variant="isConnected ? 'secondary' : 'destructive'" class="gap-1 text-[11px]">
          <component :is="isConnected ? Wifi : WifiOff" class="size-3" />
          {{ isConnected ? 'Live' : 'Offline' }}
        </Badge>
      </div>
      <SidebarMenu>
        <SidebarMenuItem>
          <DropdownMenu>
            <DropdownMenuTrigger as-child>
              <SidebarMenuButton class="h-10" :tooltip="authStore.user?.email ?? 'Account'">
                <Avatar class="size-6">
                  <AvatarFallback class="text-[10px]">{{ userInitials }}</AvatarFallback>
                </Avatar>
                <span class="truncate text-xs">{{ authStore.user?.email ?? '—' }}</span>
              </SidebarMenuButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent class="w-60 text-xs" align="end" side="top">
              <DropdownMenuItem @click="handleOpenSettings">
                <Settings class="size-4" />
                Einstellungen öffnen
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem @click="setTheme('light')">
                <Sun class="size-4" />
                <span class="flex-1">Light</span>
                <Check v-if="mode === 'light'" class="size-4" />
              </DropdownMenuItem>
              <DropdownMenuItem @click="setTheme('dark')">
                <Moon class="size-4" />
                <span class="flex-1">Dark</span>
                <Check v-if="mode === 'dark'" class="size-4" />
              </DropdownMenuItem>
              <DropdownMenuItem @click="setTheme('auto')">
                <Monitor class="size-4" />
                <span class="flex-1">Auto</span>
                <Check v-if="mode === 'auto'" class="size-4" />
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem as-child>
                <RouterLink to="/docs" class="flex items-center gap-2" @click="closeMobileSidebar">
                  <BookOpen class="size-4" />
                  Docs
                </RouterLink>
              </DropdownMenuItem>
              <DropdownMenuItem @click="handleLogout">
                <LogOut class="size-4" />
                Sign out
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarFooter>

    <SidebarRail />
  </Sidebar>

  <SearchModal v-model:open="searchOpen" />

  <Dialog :open="deleteTarget !== null" @update:open="(open) => { if (!open) deleteTarget = null }">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Delete chat?</DialogTitle>
        <DialogDescription>
          This will permanently delete
          <span class="font-medium text-foreground">{{ deleteTarget ? conversationTitle(deleteTarget) : '' }}</span>
          and abort any active run.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="deleteTarget = null">Cancel</Button>
        <Button variant="destructive" @click="confirmDelete">Delete</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
