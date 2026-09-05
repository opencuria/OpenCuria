<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useHarnessStore } from '@/stores/harness'
import { useTerminalStore } from '@/stores/terminal'
import { useDesktopStore } from '@/stores/desktop'
import { useFileExplorerStore } from '@/stores/fileExplorer'
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
import HarnessChatSidebar from '@/components/chat/HarnessChatSidebar.vue'
import WorkspaceActions from '@/components/workspaces/WorkspaceActions.vue'
import WorkspaceTerminal from '@/components/workspaces/WorkspaceTerminal.vue'
import WorkspaceDesktop from '@/components/workspaces/WorkspaceDesktop.vue'
import WorkspaceImageArtifactDialog from '@/components/workspaces/WorkspaceImageArtifactDialog.vue'
import FileExplorerPanel from '@/components/files/FileExplorerPanel.vue'
import FileViewer from '@/components/files/FileViewer.vue'
import { Badge } from '@/components/ui/badge'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ArrowLeft, Loader2, MessageSquare } from '@lucide/vue'

const route = useRoute()
const router = useRouter()
const workspaceStore = useWorkspaceStore()
const terminalStore = useTerminalStore()
const desktopStore = useDesktopStore()

const workspaceId = computed(() => route.params.id as string)
const workspace = computed(() => workspaceStore.activeWorkspace)
const fileExplorerStore = useFileExplorerStore()
const workspaceImageStore = useWorkspaceImageStore()
const renaming = ref(false)
const editingName = ref(false)
const workspaceNameInput = ref('')
const terminalHeight = ref(300)
const imageArtifactDialogOpen = ref(false)
const mobileChatListOpen = ref(false)

const lgQuery = window.matchMedia('(min-width: 1024px)')
const isDesktop = ref(lgQuery.matches)
const onBreakpointChange = (e: MediaQueryListEvent) => {
  isDesktop.value = e.matches
}

const canPrompt = computed(
  () =>
    workspace.value?.status === WorkspaceStatus.RUNNING &&
    workspace.value?.runner_online &&
    !workspace.value?.active_operation,
)
const workspaceTransitionLabel = computed(() =>
  workspaceStore.getWorkspaceTransitionLabel(workspaceId.value),
)
const isRunnerOfflineState = computed(
  () =>
    !workspace.value?.runner_online &&
    workspace.value?.status !== WorkspaceStatus.DELETED &&
    workspace.value?.status !== WorkspaceStatus.REMOVED,
)

const statusVariant = computed((): 'secondary' | 'outline' | 'destructive' | 'default' => {
  if (isRunnerOfflineState.value) {
    return 'secondary'
  }
  switch (workspace.value?.status) {
    case WorkspaceStatus.RUNNING:
      return 'secondary'
    case WorkspaceStatus.CREATING:
      return 'outline'
    case WorkspaceStatus.STOPPED:
      return 'secondary'
    case WorkspaceStatus.FAILED:
    case WorkspaceStatus.DELETE_FAILED:
      return 'destructive'
    default:
      return 'secondary'
  }
})
const statusLabel = computed(() => {
  if (isRunnerOfflineState.value) {
    return 'Runner offline'
  }
  return workspace.value?.status ?? ''
})
const showWorkspaceTransitionLabel = computed(
  () => Boolean(workspaceTransitionLabel.value) && !isRunnerOfflineState.value,
)
const autoStopCountdownLabel = computed(() =>
  workspace.value?.auto_stop_at ? `Stops ${formatRelativeTime(workspace.value.auto_stop_at)}` : null,
)
const showImminentAutoStop = computed(() => {
  if (!workspace.value?.auto_stop_at || workspace.value.status !== WorkspaceStatus.RUNNING) {
    return false
  }
  const remainingMs = new Date(workspace.value.auto_stop_at).getTime() - Date.now()
  return remainingMs > 0 && remainingMs <= 10 * 60 * 1000
})

const harnessStore = useHarnessStore()

const hasHarnessChats = computed(() => harnessStore.rootSessions.length > 0)

function handleSelectHarnessSession(sessionId: string): void {
  harnessStore.setActiveSession(sessionId)
}

function handleCreateHarnessChat(): void {
  harnessStore.setActiveSession(null)
}

async function handleRenameHarnessSession(sessionId: string, title: string): Promise<void> {
  await harnessStore.renameSession(sessionId, title)
}

async function handleDeleteHarnessSession(sessionId: string): Promise<void> {
  await harnessStore.removeSession(sessionId)
}

const isDesktopPanelVisible = computed(
  () => desktopStore.isOpen && !desktopStore.isMinimized && canPrompt.value,
)

const chatPanelTarget = computed<HTMLElement | null>(() => {
  if (isDesktopPanelVisible.value) {
    return desktopChatPanelHost.value
  }
  return mainChatPanelHost.value
})

const mainChatPanelHost = ref<HTMLElement | null>(null)
const desktopChatPanelHost = ref<HTMLElement | null>(null)

// Socket.IO event cleanup functions
const cleanupFns: (() => void)[] = []

function setupSocketListeners(): void {
  // Subscribe to workspace events
  subscribeToWorkspace(workspaceId.value)

  cleanupFns.push(
    onEvent('workspace:status_changed', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.updateWorkspaceStatus(
          data.workspace_id,
          data.status as WorkspaceStatus,
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
    onEvent('files:list_result', (data) => {
      if (data.workspace_id === workspaceId.value) {
        fileExplorerStore.handleListResult(data.request_id, data.path, data.entries, data.error)
      }
    }),
  )

  cleanupFns.push(
    onEvent('files:content_result', (data) => {
      if (data.workspace_id === workspaceId.value) {
        fileExplorerStore.handleContentResult(
          data.request_id,
          data.path,
          data.content,
          data.size,
          data.truncated,
          data.error,
        )
        // Also dispatch to image store (it ignores requests it didn't initiate)
        workspaceImageStore.handleContentResult(
          data.request_id,
          data.path,
          data.content,
          data.error,
          data.mime_type,
        )
      }
    }),
  )

  cleanupFns.push(
    onEvent('files:upload_result', (data) => {
      if (data.workspace_id === workspaceId.value) {
        fileExplorerStore.handleUploadResult(
          data.request_id,
          data.path,
          data.status,
          workspaceId.value,
          data.error,
        )
        // Also dispatch to image store so the harness composer can show upload feedback.
        workspaceImageStore.handleUploadResult(data.request_id, data.status, data.error)
      }
    }),
  )

  cleanupFns.push(
    onEvent('files:download_result', (data) => {
      if (data.workspace_id === workspaceId.value) {
        fileExplorerStore.handleDownloadResult(
          data.request_id,
          data.content,
          data.filename,
          data.is_archive,
          data.error,
        )
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

onMounted(() => {
  lgQuery.addEventListener('change', onBreakpointChange)
  start()
  setupSocketListeners()
})

watch(
  () => workspace.value?.name,
  (name) => {
    if (name && !editingName.value) {
      workspaceNameInput.value = name
    }
  },
  { immediate: true },
)

watch(isDesktop, (desktop) => {
  if (!desktop) {
    fileExplorerStore.close()
  }
})

onUnmounted(() => {
  lgQuery.removeEventListener('change', onBreakpointChange)
  stop()
  cleanupSocket()
  desktopStore.reset()
  terminalStore.reset()
  fileExplorerStore.reset()
  workspaceImageStore.reset()
  harnessStore.reset()
  workspaceStore.activeWorkspace = null
})

// React to route changes (if user navigates between workspaces)
watch(workspaceId, (newId, oldId) => {
  if (newId !== oldId) {
    cleanupSocket()
    desktopStore.reset()
    fileExplorerStore.reset()
    harnessStore.reset()
    workspaceStore.fetchWorkspaceDetail(newId)
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

function startEditingName(): void {
  if (!workspace.value || workspaceTransitionLabel.value) return
  workspaceNameInput.value = workspace.value.name
  editingName.value = true
}

function cancelEditingName(): void {
  if (workspace.value) {
    workspaceNameInput.value = workspace.value.name
  }
  editingName.value = false
}

async function saveWorkspaceName(): Promise<void> {
  if (!workspace.value || workspaceTransitionLabel.value) return

  const trimmed = workspaceNameInput.value.trim()
  if (!trimmed || trimmed === workspace.value.name) {
    cancelEditingName()
    return
  }

  renaming.value = true
  const success = await workspaceStore.renameWorkspace(workspace.value.id, trimmed)
  renaming.value = false

  if (success) {
    editingName.value = false
  }
}

// --- Terminal resize drag ---

const isDragging = ref(false)

function onDragStart(e: MouseEvent): void {
  e.preventDefault()
  isDragging.value = true
  const startY = e.clientY
  const startHeight = terminalHeight.value

  const onMove = (ev: MouseEvent) => {
    const delta = startY - ev.clientY
    terminalHeight.value = Math.max(150, Math.min(startHeight + delta, window.innerHeight * 0.7))
  }

  const onUp = () => {
    isDragging.value = false
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }

  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
</script>

<template>
  <div class="flex flex-col -m-6 lg:-m-8 h-[calc(100%+3rem)] lg:h-[calc(100%+4rem)]">
    <!-- Workspace header -->
    <div class="border-b border-border bg-header px-3 py-2.5 lg:px-6 lg:py-3 shrink-0">
      <div class="flex items-center justify-between gap-2 min-w-0">
        <!-- Left: back + workspace info -->
        <div class="flex items-center gap-2 min-w-0 flex-1">
          <Button variant="ghost" size="icon-sm" class="shrink-0" @click="goBack">
            <ArrowLeft :size="16" />
          </Button>

          <div v-if="workspace" class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 min-w-0">
              <template v-if="editingName">
                <div class="flex items-center gap-2 flex-1 min-w-0">
                  <Input
                    v-model="workspaceNameInput"
                    class="h-8 min-w-0"
                    maxlength="255"
                    @keydown.enter.prevent="saveWorkspaceName"
                    @keydown.esc.prevent="cancelEditingName"
                  />
                  <Button size="sm" :disabled="renaming" @click="saveWorkspaceName">
                    Save
                  </Button>
                  <Button size="sm" variant="outline" :disabled="renaming" @click="cancelEditingName">
                    Cancel
                  </Button>
                </div>
              </template>
              <template v-else>
                <h2 class="font-semibold text-foreground text-sm truncate">
                  {{ workspace.name }}
                </h2>
                <Badge :variant="statusVariant" class="shrink-0">
                  {{ statusLabel }}
                </Badge>
                <Badge
                  v-if="showImminentAutoStop && autoStopCountdownLabel"
                  variant="outline"
                  class="shrink-0"
                >
                  {{ autoStopCountdownLabel }}
                </Badge>
                <Badge
                  v-if="showWorkspaceTransitionLabel"
                  variant="default"
                  class="shrink-0 flex items-center gap-1"
                >
                  <Loader2 :size="12" class="animate-spin" />
                  {{ workspaceTransitionLabel }}
                </Badge>
                <button
                  class="hidden sm:flex items-center text-xs text-muted-foreground hover:text-foreground transition-colors shrink-0 px-1"
                  :disabled="showWorkspaceTransitionLabel"
                  @click="startEditingName"
                >
                  Rename
                </button>
              </template>
            </div>
            <div class="hidden sm:flex items-center gap-3 mt-0.5">
              <span class="text-xs text-muted-foreground font-mono">{{ workspace.id.slice(0, 12) }}…</span>
            </div>
          </div>
        </div>

        <!-- Right: mobile chats button + workspace actions -->
        <div class="flex items-center gap-1 shrink-0">
          <Button
            v-if="hasHarnessChats"
            variant="ghost"
            size="icon-sm"
            class="md:hidden"
            title="Switch chat"
            @click="mobileChatListOpen = true"
          >
            <MessageSquare :size="16" />
          </Button>
          <WorkspaceActions
            v-if="workspace"
            :workspace="workspace"
            @capture-image="imageArtifactDialogOpen = true"
            hide-destructive
          />
        </div>
      </div>
    </div>

    <!-- Loading state -->
    <div v-if="workspaceStore.loading && !workspace" class="flex-1 flex items-center justify-center">
      <LoadingSpinner :size="24" />
    </div>

    <!-- Chat area + terminal -->
    <template v-else-if="workspace">
      <div class="flex flex-col flex-1 min-h-0">
        <!-- Chat content area -->
        <div class="flex flex-1 min-h-0">
          <WorkspaceDesktop
            v-if="isDesktopPanelVisible"
            :workspace-id="workspaceId"
          >
            <template #sidebar-content>
              <div ref="desktopChatPanelHost" class="h-full min-h-0 w-full"></div>
            </template>
          </WorkspaceDesktop>

          <template v-else>
            <HarnessChatSidebar
              :sessions="harnessStore.rootSessions"
              :child-sessions-by-parent="harnessStore.childSessionsByParent"
              :active-session-id="harnessStore.activeSessionId"
              :mobile-open="mobileChatListOpen"
              @select="handleSelectHarnessSession"
              @create="handleCreateHarnessChat"
              @rename="handleRenameHarnessSession"
              @delete="handleDeleteHarnessSession"
              @close="mobileChatListOpen = false"
            />

            <!-- Harness chat area -->
            <div class="flex flex-col flex-1 min-w-0 overflow-x-hidden">
              <FileViewer
                v-if="fileExplorerStore.isViewingFile || fileExplorerStore.isLoadingContent"
                :workspace-id="workspaceId"
              />
              <div
                v-else
                ref="mainChatPanelHost"
                class="min-h-0 flex flex-1 min-w-0 overflow-hidden"
              ></div>
            </div>

            <!-- File explorer panel (right side) -->
            <FileExplorerPanel
              v-if="isDesktop && fileExplorerStore.isOpen && canPrompt"
              :workspace-id="workspaceId"
            />
          </template>
        </div>

        <!-- Terminal panel (bottom) -->
        <template v-if="terminalStore.isOpen && canPrompt">
          <!-- Drag handle -->
          <div
            v-show="!terminalStore.isMinimized"
            class="h-1 bg-border hover:bg-primary cursor-row-resize shrink-0 transition-colors"
            @mousedown="onDragStart"
          ></div>
          <div
            v-show="!terminalStore.isMinimized"
            class="shrink-0 relative"
            :style="{ height: terminalHeight + 'px' }"
          >
            <WorkspaceTerminal :workspace-id="workspaceId" />
          </div>
        </template>


        <Teleport v-if="chatPanelTarget" :to="chatPanelTarget">
          <HarnessChatPanel
            :workspace-id="workspaceId"
            :can-prompt="canPrompt"
            :show-workspace-toolbar="isDesktop && !isDesktopPanelVisible"
            class="min-h-0 flex-1"
          />
        </Teleport>
      </div>
    </template>

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
