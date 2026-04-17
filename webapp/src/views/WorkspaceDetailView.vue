<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useWorkspaceStore } from '@/stores/workspaces'
import { PENDING_CHAT_ID } from '@/stores/workspaces'
import { useConversationStore } from '@/stores/conversations'
import { useSkillStore } from '@/stores/skills'
import { useTerminalStore } from '@/stores/terminal'
import { useDesktopStore } from '@/stores/desktop'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import * as agentsApi from '@/services/agents.api'
import { usePolling } from '@/composables/usePolling'
import { useChatOptionsCache } from '@/composables/useChatOptionsCache'
import {
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
  onEvent,
} from '@/services/socket'
import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { Agent } from '@/types'
import { isSessionActive, isSessionDone } from '@/lib/sessionState'
import { findLatestUnreadDoneSession, isSessionEligibleForAutoRead } from '@/lib/sessionReadState'
import { formatRelativeTime } from '@/lib/utils'
import ChatContainer from '@/components/chat/ChatContainer.vue'
import ChatInput from '@/components/chat/ChatInput.vue'
import ChatSidebar from '@/components/chat/ChatSidebar.vue'
import WorkspaceActions from '@/components/workspaces/WorkspaceActions.vue'
import WorkspaceTerminal from '@/components/workspaces/WorkspaceTerminal.vue'
import WorkspaceDesktop from '@/components/workspaces/WorkspaceDesktop.vue'
import WorkspaceImageArtifactDialog from '@/components/workspaces/WorkspaceImageArtifactDialog.vue'
import FileExplorerPanel from '@/components/files/FileExplorerPanel.vue'
import FileViewer from '@/components/files/FileViewer.vue'
import { UiBadge, UiDialog, UiSpinner, UiButton, UiInput } from '@/components/ui'
import { ArrowLeft, Bot, TerminalSquare, FolderTree, Monitor, MessageSquare, CheckCircle, AlertCircle, ChevronDown, ChevronUp, Loader2 } from 'lucide-vue-next'

const route = useRoute()
const router = useRouter()
const workspaceStore = useWorkspaceStore()
const conversationStore = useConversationStore()
const skillStore = useSkillStore()
const terminalStore = useTerminalStore()
const desktopStore = useDesktopStore()

const workspaceId = computed(() => route.params.id as string)
const workspace = computed(() => workspaceStore.activeWorkspace)
const fileExplorerStore = useFileExplorerStore()
const workspaceImageStore = useWorkspaceImageStore()
const chatInputRef = ref<InstanceType<typeof ChatInput> | null>(null)
const sending = ref(false)
const renaming = ref(false)
const editingName = ref(false)
const workspaceNameInput = ref('')
const agents = ref<Agent[]>([])
const selectedOptions = ref<Record<string, string>>({})
const selectedOptionsInitializedChatId = ref<string | null>(null)
const terminalHeight = ref(300)
const imageArtifactDialogOpen = ref(false)
const loadingChats = ref(false)
const mainChatPanelHost = ref<HTMLElement | null>(null)
const desktopChatPanelHost = ref<HTMLElement | null>(null)

const lgQuery = window.matchMedia('(min-width: 1024px)')
const isDesktop = ref(lgQuery.matches)
const onBreakpointChange = (e: MediaQueryListEvent) => {
  isDesktop.value = e.matches
}

const mobileChatListOpen = ref(false)

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
  () => !workspace.value?.runner_online && workspace.value?.status !== WorkspaceStatus.REMOVED,
)

// Workspaces always support multiple chats now (agent is per-chat)
const isMultiChat = computed(() => true)

const hasActiveSession = computed(() => {
  // Workspace-level flag from API covers all chats in this workspace
  if (workspace.value?.has_active_session) return true
  // Also check local session state for immediate optimistic feedback
  const sessions = workspaceStore.activeChatSessions
  return sessions.some((s) => isSessionActive(s.status))
})

const statusVariant = computed(() => {
  if (isRunnerOfflineState.value) {
    return 'muted'
  }
  switch (workspace.value?.status) {
    case WorkspaceStatus.RUNNING:
      return 'success'
    case WorkspaceStatus.CREATING:
      return 'warning'
    case WorkspaceStatus.STOPPED:
      return 'muted'
    case WorkspaceStatus.FAILED:
      return 'error'
    default:
      return 'muted'
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
const hasChats = computed(() => workspaceStore.chats.length > 0)
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
const showWorkspaceEmptyState = computed(
  () =>
    Boolean(workspace.value) &&
    !workspaceStore.loading &&
    !loadingChats.value &&
    !hasChats.value &&
    !fileExplorerStore.isViewingFile &&
    !fileExplorerStore.isLoadingContent,
)

const currentAgent = computed(() => {
  const agentDefinitionId = workspaceStore.activeChat?.agent_definition_id
  return agents.value.find((a) => a.id === agentDefinitionId) ?? null
})

const agentOptions = computed(() => currentAgent.value?.available_options ?? [])

function getModelChoices(agent: Agent | null): string[] {
  if (!agent) return []
  const modelOption = agent.available_options.find((option) => option.key === 'model')
  if (modelOption && modelOption.choices.length > 0) return modelOption.choices
  return []
}

const isActiveChatWritable = computed(() => {
  const chat = workspaceStore.activeChat
  if (!chat || chat.is_pending) return true
  const agent = currentAgent.value
  if (!agent || agent.supports_multi_chat) return true

  const latestChat = workspaceStore.chats
    .filter((item) => !item.is_pending && item.agent_definition_id === chat.agent_definition_id)
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())[0]
  return latestChat?.id === chat.id
})

const chatLockMessage = computed(() => {
  if (!isActiveChatWritable.value) {
    return 'This chat is locked because a newer chat exists for this single-chat agent.'
  }
  if (workspaceTransitionLabel.value) {
    return `${workspaceTransitionLabel.value} Please wait.`
  }
  if (hasActiveSession.value) {
    return 'Another session is running — please wait.'
  }
  return undefined
})

// Agent picker state for creating new chats within the workspace
const showAgentPicker = ref(false)
const showOtherAgents = ref(false)
const pendingPrompt = ref<{ prompt: string; options: Record<string, string>; skillIds: string[] } | null>(null)
const suppressedAutoReadSessionIds = ref<Set<string>>(new Set())

const availableAgents = computed(() =>
  agents.value.filter(a => a.has_online_runner && a.has_credentials)
)

const secondaryAgents = computed(() =>
  agents.value.filter(a => !(a.has_online_runner && a.has_credentials))
)

const { loadFromCache: loadCachedChatOptions, saveToCache: saveCachedChatOptions } =
  useChatOptionsCache(workspaceId, () => workspaceStore.activeChatId)

async function loadAvailableAgents(id: string): Promise<void> {
  try {
    agents.value = await agentsApi.listAgents(id)
  } catch {
    agents.value = []
  }
}

async function loadWorkspaceChats(
  id: string,
  preferredChatId?: string,
): Promise<void> {
  loadingChats.value = true
  try {
    await workspaceStore.fetchChats(id)
    if (preferredChatId && workspaceStore.chats.some((chat) => chat.id === preferredChatId)) {
      workspaceStore.setActiveChat(preferredChatId)
    }
  } finally {
    loadingChats.value = false
  }
}

async function loadActiveChatSessions(): Promise<void> {
  const chatId = workspaceStore.activeChatId
  if (!chatId || chatId === PENDING_CHAT_ID) {
    workspaceStore.activeSessions = []
    return
  }
  await workspaceStore.fetchChatSessions(workspaceId.value, chatId)
}

function handleAgentPickerOpenChange(isOpen: boolean): void {
  showAgentPicker.value = isOpen
  if (!isOpen) {
    showOtherAgents.value = false
    pendingPrompt.value = null
  }
}

function openFileExplorer(): void {
  if (!canPrompt.value) return
  fileExplorerStore.open()
}

function openTerminal(): void {
  if (!canPrompt.value) return
  terminalStore.open()
}

function handleTerminalButtonClick(): void {
  if (!canPrompt.value) return
  if (!terminalStore.isOpen) {
    terminalStore.open()
    return
  }
  if (terminalStore.isMinimized) {
    terminalStore.restore()
    return
  }
  terminalStore.minimize()
}

const terminalButtonTitle = computed(() => {
  if (!terminalStore.isOpen) return 'Open terminal'
  if (terminalStore.isMinimized) return 'Restore terminal'
  return 'Minimize terminal'
})

function openDesktopPanel(): void {
  if (!canPrompt.value) return
  desktopStore.open()
}

function handleDesktopButtonClick(): void {
  if (!canPrompt.value) return
  if (!desktopStore.isOpen) {
    desktopStore.open()
    return
  }
  if (desktopStore.isMinimized) {
    desktopStore.restore()
    return
  }
  desktopStore.minimize()
}

const desktopButtonTitle = computed(() => {
  if (!desktopStore.isOpen) return 'Open desktop'
  if (desktopStore.isMinimized) return 'Restore desktop'
  return 'Minimize desktop'
})

const isDesktopPanelVisible = computed(
  () => desktopStore.isOpen && !desktopStore.isMinimized && canPrompt.value,
)

const chatPanelTarget = computed<HTMLElement | null>(() => {
  if (isDesktopPanelVisible.value) {
    return desktopChatPanelHost.value
  }
  return mainChatPanelHost.value
})

// Socket.IO event cleanup functions
const cleanupFns: (() => void)[] = []

function setupSocketListeners(): void {
  // Subscribe to workspace events
  subscribeToWorkspace(workspaceId.value)

  cleanupFns.push(
    onEvent('session:output_chunk', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.appendOutputChunk(data.session_id, data.line)
      }
    }),
  )

  cleanupFns.push(
    onEvent('session:status', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.setSessionStatusDetail(data.session_id, data.detail)
      }
    }),
  )

  cleanupFns.push(
    onEvent('session:completed', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.markSessionComplete(data.session_id)
        markSessionInViewAsRead(data.session_id)
      }
    }),
  )

  cleanupFns.push(
    onEvent('session:failed', (data) => {
      if (data.workspace_id === workspaceId.value) {
        workspaceStore.markSessionFailed(data.session_id, data.error)
        markSessionInViewAsRead(data.session_id)
      }
    }),
  )

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
        // Also dispatch to image store so ChatInput can show upload feedback.
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
  skillStore.fetchSkills()
  loadAvailableAgents(workspaceId.value)

  // Always fetch chats (workspaces now always support multiple chats)
  void loadWorkspaceChats(workspaceId.value, route.query.chatId as string | undefined)
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
  workspaceStore.activeWorkspace = null
  workspaceStore.chats = []
  workspaceStore.activeChatId = null
  workspaceStore.activeSessions = []
  workspaceStore.supportsMultiChat = false
  loadingChats.value = false
})

// React to route changes (if user navigates between workspaces)
watch(workspaceId, (newId, oldId) => {
  if (newId !== oldId) {
    cleanupSocket()
    desktopStore.reset()
    fileExplorerStore.reset()
    workspaceStore.chats = []
    workspaceStore.activeChatId = null
    workspaceStore.activeSessions = []
    loadingChats.value = true
    workspaceStore.fetchWorkspaceDetail(newId)
    loadAvailableAgents(newId)
    void loadWorkspaceChats(newId, route.query.chatId as string | undefined)
    setupSocketListeners()
  }
})

// React to chatId query param changes (e.g. navigating from the dashboard)
watch(
  () => route.query.chatId as string | undefined,
  (chatId) => {
    if (chatId && workspaceStore.chats.some((c) => c.id === chatId)) {
      workspaceStore.setActiveChat(chatId)
    }
  },
)

watch(
  () => workspaceStore.activeChatId,
  () => {
    void loadActiveChatSessions()
  },
  { immediate: true },
)

// Initialise selectedOptions from the active chat context:
// - existing chat with messages -> reuse last used values (model included)
// - chat without messages -> use defaults
watch(
  [agentOptions, () => workspaceStore.activeChatId, () => workspaceStore.activeChatSessions],
  () => {
    const opts = agentOptions.value
    const activeChatId = workspaceStore.activeChatId
    if (!opts.length || !activeChatId || !workspace.value) return

    if (selectedOptionsInitializedChatId.value === activeChatId) return

    const sessions = workspaceStore.activeChatSessions ?? []
    const sortedSessions = [...sessions].sort(
      (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    )
    const latestSession = sortedSessions[0]
    const latestSessionOptions = sortedSessions.find(
      (s) => s.agent_options && Object.keys(s.agent_options).length,
    )?.agent_options ?? {}

    const cachedOptions = loadCachedChatOptions()
    const resolved: Record<string, string> = {}
    for (const opt of opts) {
      const cachedVal = cachedOptions[opt.key] || ''
      const lastVal = latestSessionOptions[opt.key] || ''

      if (cachedVal && opt.choices.includes(cachedVal)) {
        resolved[opt.key] = cachedVal
        continue
      }

      if (opt.key === 'model') {
        const latestModel = latestSession?.agent_model || ''
        if (sortedSessions.length > 0 && lastVal && opt.choices.includes(lastVal)) {
          resolved[opt.key] = lastVal
        } else if (sortedSessions.length > 0 && latestModel && opt.choices.includes(latestModel)) {
          resolved[opt.key] = latestModel
        } else {
          resolved[opt.key] = opt.default
        }
        continue
      }

      if (sortedSessions.length > 0 && lastVal && opt.choices.includes(lastVal)) {
        resolved[opt.key] = lastVal
      } else {
        resolved[opt.key] = opt.default
      }
    }
    selectedOptions.value = resolved
    selectedOptionsInitializedChatId.value = activeChatId
  },
  { immediate: true },
)

watch(
  [selectedOptions, () => workspaceStore.activeChatId],
  ([options, activeChatId]) => {
    if (!activeChatId) return
    saveCachedChatOptions(options)
  },
  { deep: true },
)

function goBack(): void {
  if (window.history.state?.back) {
    router.back()
  } else {
    router.push('/workspaces')
  }
}

async function handleSend(
  prompt: string,
  options: Record<string, string>,
  skillIds: string[],
): Promise<void> {
  // No active chat yet — ask user to pick an agent first, then auto-send
  if (!workspaceStore.activeChatId) {
    pendingPrompt.value = { prompt, options, skillIds }
    showAgentPicker.value = true
    showOtherAgents.value = false
    // ChatInput clears itself on emit — restore the text so it stays visible
    await nextTick()
    chatInputRef.value?.restorePrompt(prompt)
    return
  }
  sending.value = true
  await workspaceStore.sendPrompt(workspaceId.value, prompt, options, undefined, skillIds)
  sending.value = false
}

async function handleStopPrompt(): Promise<void> {
  sending.value = true
  try {
    await workspaceStore.cancelActivePrompt(workspaceId.value)
  } finally {
    sending.value = false
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

// --- Chat handlers ---

function handleSelectChat(chatId: string): void {
  suppressedAutoReadSessionIds.value = new Set()
  workspaceStore.setActiveChat(chatId)
}

function handleCreateChat(): void {
  // Close mobile sidebar before showing the agent picker
  mobileChatListOpen.value = false
  showAgentPicker.value = true
  showOtherAgents.value = false
}

async function handleCreateChatWithAgent(agentId: string, agentName: string): Promise<void> {
  if (!workspace.value) return
  showAgentPicker.value = false
  workspaceStore.createChat(workspace.value.id, {
    agent_definition_id: agentId,
    agent_type: agentName,
  })

  if (pendingPrompt.value) {
    const { prompt, options, skillIds } = pendingPrompt.value
    pendingPrompt.value = null
    chatInputRef.value?.clearInput()
    sending.value = true
    try {
      await workspaceStore.sendPrompt(workspaceId.value, prompt, options, undefined, skillIds)
    } finally {
      sending.value = false
    }
  }
}

async function handleRenameChat(chatId: string, name: string): Promise<void> {
  if (!workspace.value) return
  await workspaceStore.renameChatAction(workspace.value.id, chatId, name)
}

async function handleDeleteChat(chatId: string): Promise<void> {
  if (!workspace.value) return
  await workspaceStore.deleteChatAction(workspace.value.id, chatId)
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

/** Sessions to display: scoped to active chat in multi-chat mode. */
const displaySessions = computed(() => {
  return workspaceStore.activeChatSessions
})

const activeConversationKey = computed(() => `${workspaceId.value}:${workspaceStore.activeChatId ?? 'workspace'}`)

/**
 * Mark the currently visible conversation as read. Finds the most recent
 * completed or failed session in the displayed view and calls markAsRead.
 */
function markCurrentViewAsRead(): void {
  const lastDone = findLatestUnreadDoneSession(
    displaySessions.value,
    suppressedAutoReadSessionIds.value,
  )
  if (!lastDone) return

  lastDone.read_at = new Date().toISOString()
  conversationStore.markAsRead(workspaceId.value, lastDone.chat_id ?? null, lastDone.id)
}

function markSessionInViewAsRead(sessionId: string): void {
  const session = displaySessions.value.find((item) => item.id === sessionId)
  if (!session || !isSessionEligibleForAutoRead(session, suppressedAutoReadSessionIds.value)) return

  session.read_at = new Date().toISOString()
  conversationStore.markAsRead(workspaceId.value, session.chat_id ?? null, session.id)
}

watch(
  activeConversationKey,
  () => {
    suppressedAutoReadSessionIds.value = new Set()
    markCurrentViewAsRead()
  },
  { immediate: true },
)

watch(
  () => displaySessions.value.length,
  () => {
    markCurrentViewAsRead()
  },
)

function handleToggleSessionReadState(sessionId: string): void {
  const session = displaySessions.value.find((item) => item.id === sessionId)
  if (!session || !isSessionDone(session.status)) return

  if (session.read_at) {
    suppressedAutoReadSessionIds.value = new Set(suppressedAutoReadSessionIds.value).add(sessionId)
    session.read_at = null
    conversationStore.markAsUnread(workspaceId.value, session.chat_id ?? null, session.id)
    return
  }

  const nextSuppressed = new Set(suppressedAutoReadSessionIds.value)
  nextSuppressed.delete(sessionId)
  suppressedAutoReadSessionIds.value = nextSuppressed
  session.read_at = new Date().toISOString()
  conversationStore.markAsRead(workspaceId.value, session.chat_id ?? null, session.id)
}
</script>

<template>
  <div class="flex flex-col -m-6 lg:-m-8 h-[calc(100%+3rem)] lg:h-[calc(100%+4rem)]">
    <!-- Workspace header -->
    <div class="border-b border-border bg-surface px-3 py-2.5 lg:px-6 lg:py-3 shrink-0">
      <div class="flex items-center justify-between gap-2 min-w-0">
        <!-- Left: back + workspace info -->
        <div class="flex items-center gap-2 min-w-0 flex-1">
          <UiButton variant="ghost" size="icon-sm" class="shrink-0" @click="goBack">
            <ArrowLeft :size="16" />
          </UiButton>

          <div v-if="workspace" class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 min-w-0">
              <template v-if="editingName">
                <div class="flex items-center gap-2 flex-1 min-w-0">
                  <UiInput
                    v-model="workspaceNameInput"
                    class="h-8 min-w-0"
                    maxlength="255"
                    @keydown.enter.prevent="saveWorkspaceName"
                    @keydown.esc.prevent="cancelEditingName"
                  />
                  <UiButton size="sm" :disabled="renaming" @click="saveWorkspaceName">
                    Save
                  </UiButton>
                  <UiButton size="sm" variant="outline" :disabled="renaming" @click="cancelEditingName">
                    Cancel
                  </UiButton>
                </div>
              </template>
              <template v-else>
                <h2 class="font-semibold text-fg text-sm truncate">
                  {{ workspace.name }}
                </h2>
                <UiBadge :variant="statusVariant" class="shrink-0">
                  {{ statusLabel }}
                </UiBadge>
                <UiBadge
                  v-if="showImminentAutoStop && autoStopCountdownLabel"
                  variant="warning"
                  class="shrink-0"
                >
                  {{ autoStopCountdownLabel }}
                </UiBadge>
                <UiBadge
                  v-if="showWorkspaceTransitionLabel"
                  variant="info"
                  class="shrink-0 flex items-center gap-1"
                >
                  <Loader2 :size="12" class="animate-spin" />
                  {{ workspaceTransitionLabel }}
                </UiBadge>
                <button
                  class="hidden sm:flex items-center text-xs text-muted-fg hover:text-fg transition-colors shrink-0 px-1"
                  :disabled="showWorkspaceTransitionLabel"
                  @click="startEditingName"
                >
                  Rename
                </button>
              </template>
            </div>
            <div class="hidden sm:flex items-center gap-3 mt-0.5">
              <span class="text-xs text-muted-fg font-mono">{{ workspace.id.slice(0, 12) }}…</span>
              <span v-if="currentAgent" class="flex items-center gap-1 text-xs text-muted-fg">
                <Bot :size="12" />
                {{ currentAgent.description || currentAgent.name }}
              </span>
            </div>
          </div>
        </div>

        <!-- Right: mobile chats button + workspace actions -->
        <div class="flex items-center gap-1 shrink-0">
          <!-- Mobile chat list toggle (only for multi-chat agents) -->
          <UiButton
            v-if="isMultiChat && hasChats"
            variant="ghost"
            size="icon-sm"
            class="md:hidden"
            title="Switch chat"
            @click="mobileChatListOpen = true"
          >
            <MessageSquare :size="16" />
          </UiButton>
          <WorkspaceActions
            v-if="workspace"
            :workspace="workspace"
            @capture-image="imageArtifactDialogOpen = true"
            hide-destructive
          />
        </div>
      </div>
    </div>

    <UiDialog
      :open="showAgentPicker"
      title="Choose Agent"
      description="Select the AI agent for the new chat in this workspace."
      @update:open="handleAgentPickerOpenChange"
    >
      <div class="space-y-3">
        <div v-if="availableAgents.length > 0" class="space-y-2">
          <UiButton
            v-for="agent in availableAgents"
            :key="agent.id"
            variant="ghost"
            class="h-auto w-full justify-start rounded-xl border border-border bg-transparent px-4 py-3 text-left hover:border-primary hover:bg-primary/5"
            @click="handleCreateChatWithAgent(agent.id, agent.name)"
          >
            <div class="flex w-full items-start gap-3">
              <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary/10">
                <Bot :size="16" class="text-primary" />
              </div>
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2">
                  <span class="truncate text-sm font-medium text-fg">
                    {{ agent.description || agent.name }}
                  </span>
                  <CheckCircle :size="14" class="shrink-0 text-success" />
                </div>
                <span class="text-xs text-muted-fg">
                  {{ agent.supports_multi_chat ? 'Multi-chat' : 'Single-chat' }}
                  <template v-if="getModelChoices(agent).length > 0">
                    · {{ getModelChoices(agent).length }} model{{ getModelChoices(agent).length !== 1 ? 's' : '' }}
                  </template>
                </span>
              </div>
            </div>
          </UiButton>
        </div>
        <div
          v-else
          class="rounded-xl border border-border bg-muted/30 px-4 py-3 text-sm text-muted-fg"
        >
          No agents are currently available in this workspace. The runner must be online and the required credentials must already be attached.
        </div>

        <div v-if="secondaryAgents.length > 0">
          <UiButton
            variant="ghost"
            size="sm"
            class="w-full justify-start px-0 text-xs text-muted-fg hover:text-fg"
            @click="showOtherAgents = !showOtherAgents"
          >
            <component :is="showOtherAgents ? ChevronUp : ChevronDown" :size="14" />
            {{ showOtherAgents ? 'Hide' : 'Show' }} other agents ({{ secondaryAgents.length }})
          </UiButton>
          <div v-if="showOtherAgents" class="mt-2 space-y-2">
            <UiButton
              v-for="agent in secondaryAgents"
              :key="agent.id"
              variant="ghost"
              class="h-auto w-full justify-start rounded-xl border border-border bg-transparent px-4 py-3 text-left opacity-75 hover:bg-surface-hover"
              @click="handleCreateChatWithAgent(agent.id, agent.name)"
            >
              <div class="flex w-full items-start gap-3">
                <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted">
                  <Bot :size="16" class="text-muted-fg" />
                </div>
                <div class="min-w-0 flex-1">
                  <div class="flex items-center gap-2">
                    <span class="truncate text-sm font-medium text-fg">
                      {{ agent.description || agent.name }}
                    </span>
                    <AlertCircle :size="14" class="shrink-0 text-warning" />
                  </div>
                  <span class="text-xs text-muted-fg">
                    {{ !agent.has_online_runner ? 'No runner available' : 'Missing workspace credentials' }}
                  </span>
                </div>
              </div>
            </UiButton>
          </div>
        </div>
      </div>
    </UiDialog>

    <!-- Loading state -->
    <div v-if="workspaceStore.loading && !workspace" class="flex-1 flex items-center justify-center">
      <UiSpinner :size="24" />
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
            <!-- Chat sidebar (only for multi-chat agents) -->
            <ChatSidebar
              v-if="isMultiChat && hasChats"
              :chats="workspaceStore.chats"
              :active-chat-id="workspaceStore.activeChatId"
              :mobile-open="mobileChatListOpen"
              @select="handleSelectChat"
              @create="handleCreateChat"
              @rename="handleRenameChat"
              @delete="handleDeleteChat"
              @close="mobileChatListOpen = false"
            />

            <!-- Chat content area -->
            <div class="flex flex-col flex-1 min-w-0 overflow-x-hidden">
            <FileViewer
              v-if="fileExplorerStore.isViewingFile || fileExplorerStore.isLoadingContent"
              :workspace-id="workspaceId"
            />
            <div
              v-else-if="showWorkspaceEmptyState"
              class="flex min-h-0 flex-1 items-center justify-center overflow-y-auto px-6 py-10"
            >
              <div class="w-full max-w-3xl rounded-[2rem] border border-border/80 bg-[radial-gradient(circle_at_top,rgba(255,255,255,0.14),transparent_55%),linear-gradient(160deg,rgba(255,255,255,0.08),rgba(255,255,255,0.02))] p-6 shadow-[0_30px_80px_rgba(15,23,42,0.18)] sm:p-10">
                <div class="mx-auto flex max-w-xl flex-col items-center text-center">
                  <div class="mb-5 flex h-18 w-18 items-center justify-center rounded-[1.5rem] border border-primary/20 bg-primary/10 text-primary shadow-[0_16px_40px_rgba(59,130,246,0.18)]">
                    <MessageSquare :size="30" />
                  </div>
                  <p class="mb-2 text-xs font-semibold uppercase tracking-[0.28em] text-primary/80">
                    Fresh Workspace
                  </p>
                  <h3 class="text-2xl font-semibold tracking-tight text-fg sm:text-3xl">
                    No chats yet in this workspace
                  </h3>
                  <p class="mt-3 max-w-lg text-sm leading-6 text-muted-fg sm:text-base">
                    Start the first chat to pick an agent for this workspace.
                  </p>
                  <div class="mt-8 flex flex-wrap items-center justify-center gap-3">
                    <UiButton size="lg" class="min-w-52" @click="handleCreateChat">
                      <Bot :size="16" class="mr-2" />
                      Start First Chat
                    </UiButton>
                    <UiButton
                      variant="outline"
                      size="lg"
                      :disabled="!canPrompt"
                      @click="openFileExplorer"
                    >
                      <FolderTree :size="16" class="mr-2" />
                      Open Files
                    </UiButton>
                    <UiButton
                      variant="outline"
                      size="lg"
                      :disabled="!canPrompt"
                      @click="openTerminal"
                    >
                      <TerminalSquare :size="16" class="mr-2" />
                      Open Terminal
                    </UiButton>
                    <UiButton
                      variant="outline"
                      size="lg"
                      :disabled="!canPrompt"
                      @click="openDesktopPanel"
                    >
                      <Monitor :size="16" class="mr-2" />
                      Open Desktop
                    </UiButton>
                  </div>
                </div>
              </div>
            </div>
            <div
              v-else-if="hasChats || loadingChats"
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
          <div class="flex h-full min-h-0 w-full flex-col">
            <ChatContainer
              :key="workspaceStore.activeChatId ?? 'default'"
              :sessions="displaySessions"
              :is-multi-chat="isMultiChat"
              :workspace-id="workspaceId"
              class="min-h-0 flex-1"
              @toggle-read-state="handleToggleSessionReadState"
            />
            <div
              class="flex items-center gap-0 min-w-0 overflow-x-hidden"
              :class="isDesktopPanelVisible ? 'pt-2' : ''"
            >
              <ChatInput
                ref="chatInputRef"
                :agent-options="agentOptions"
                :selected-options="selectedOptions"
                :skill-options="skillStore.skills"
                :disabled="!canPrompt || hasActiveSession || !isActiveChatWritable"
                :stoppable="canPrompt && hasActiveSession"
                :sending="sending"
                :workspace-id="workspaceId"
                :chat-id="workspaceStore.activeChatId"
                :busy-message="chatLockMessage"
                class="flex-1"
                @update:selected-options="selectedOptions = $event"
                @send="handleSend"
                @stop="handleStopPrompt"
              />
              <template v-if="isDesktop && !isDesktopPanelVisible">
                <UiButton
                  variant="ghost"
                  size="icon-sm"
                  class="shrink-0 mb-2"
                  :disabled="!canPrompt"
                  :title="fileExplorerStore.isOpen ? 'Hide files' : 'Open file explorer'"
                  @click="fileExplorerStore.toggle()"
                >
                  <FolderTree :size="16" :class="fileExplorerStore.isOpen ? 'text-primary' : ''" />
                </UiButton>
                <UiButton
                  variant="ghost"
                  size="icon-sm"
                  class="shrink-0 mr-2 mb-2"
                  :disabled="!canPrompt"
                  :title="terminalButtonTitle"
                  @click="handleTerminalButtonClick"
                >
                  <span class="relative inline-flex">
                    <TerminalSquare :size="16" :class="terminalStore.isOpen ? 'text-primary' : ''" />
                    <span
                      v-if="terminalStore.isOpen && terminalStore.isMinimized"
                      class="absolute -bottom-1 -right-1 h-2 w-2 rounded-full bg-primary"
                      title="Terminal minimized"
                    />
                  </span>
                </UiButton>
                <UiButton
                  variant="ghost"
                  size="icon-sm"
                  class="shrink-0 mr-2 mb-2"
                  :disabled="!canPrompt"
                  :title="desktopButtonTitle"
                  @click="handleDesktopButtonClick"
                >
                  <span class="relative inline-flex">
                    <Monitor :size="16" :class="desktopStore.isOpen ? 'text-primary' : ''" />
                    <span
                      v-if="desktopStore.isOpen && desktopStore.isMinimized"
                      class="absolute -bottom-1 -right-1 h-2 w-2 rounded-full bg-primary"
                      title="Desktop minimized"
                    />
                  </span>
                </UiButton>
              </template>
            </div>
          </div>
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
        <UiButton variant="outline" @click="goBack">
          Back
        </UiButton>
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
