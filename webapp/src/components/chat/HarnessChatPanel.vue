<script setup lang="ts">
import { computed, onMounted, onUnmounted, provide, ref, toRef, watch } from 'vue'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useRoute } from 'vue-router'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useHarnessStore } from '@/stores/harness'
import { useSkillStore } from '@/stores/skills'
import { useDesktopStore } from '@/stores/desktop'
import { useTerminalStore } from '@/stores/terminal'
import { onEvent, subscribeToWorkspace, unsubscribeFromWorkspace } from '@/services/socket'
import type { HarnessSessionMode } from '@/types/harness'
import type { MentionCandidate } from '@/lib/harnessMentions'
import { buildComposerSheets, type ContextSheetState } from '@/lib/composerSheets'
import { resolveSessionUsedTokens } from '@/lib/sessionContextUsage'
import { Button } from '@/components/ui/button'
import { FolderTree, Monitor, TerminalSquare } from '@lucide/vue'
import HarnessChatContainer from '@/components/chat/HarnessChatContainer.vue'
import HarnessChatInput from '@/components/chat/HarnessChatInput.vue'
import HarnessSheetStack from '@/components/chat/HarnessSheetStack.vue'

const props = defineProps<{
  workspaceId: string
  canPrompt?: boolean
  showWorkspaceToolbar?: boolean
}>()

provide(harnessWorkspaceIdKey, toRef(props, 'workspaceId'))

const harness = useHarnessStore()
const route = useRoute()
const fileExplorer = useFileExplorerStore()
const skillStore = useSkillStore()
const terminalStore = useTerminalStore()
const desktopStore = useDesktopStore()

const sending = ref(false)
const resolving = ref(false)
const answeringQuestion = ref(false)
const composerMode = ref<HarnessSessionMode>('build')

const activeSession = computed(() => harness.activeSession)
const streamingSessionId = computed(() =>
  activeSession.value?.status === 'busy' ? activeSession.value.id : null,
)

const childSessionIds = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const session of harness.sessions) {
    if (!session.parent_id) continue
    for (const message of harness.messagesBySession[session.parent_id] ?? []) {
      for (const part of message.parts) {
        if (part.type !== 'subtask') continue
        const subtaskId = part.meta?.['subtask_id']
        if (typeof subtaskId === 'string' && subtaskId) {
          map[subtaskId] = session.id
        }
      }
    }
  }
  for (const session of harness.sessions) {
    for (const message of harness.messagesBySession[session.id] ?? []) {
      for (const part of message.parts) {
        if (part.type !== 'subtask') continue
        const childId = part.meta?.['child_session_id']
        const subtaskId = part.meta?.['subtask_id']
        if (typeof childId === 'string' && childId && typeof subtaskId === 'string') {
          map[subtaskId] = childId
        }
      }
    }
  }
  return map
})

const activeRequests = computed(() => harness.activePermissionRequests)
const activeQuestions = computed(() => harness.activeQuestionRequests)

/** `@` mention mirror from the chat input (renders as the topmost stack sheet). */
const mentionActive = ref(false)
const mentionActiveIndex = ref(0)
const mentionCandidates = ref<MentionCandidate[]>([])

const contextOpen = ref(false)
const contextMetrics = ref<Pick<ContextSheetState, 'used' | 'limit' | 'percent'> | null>(null)

const contextUsed = computed(
  () => resolveSessionUsedTokens(harness.activeMessages).used,
)

const contextSheet = computed<ContextSheetState | null>(() => {
  if (!contextMetrics.value) return null
  const breakdown = resolveSessionUsedTokens(harness.activeMessages)
  return {
    ...contextMetrics.value,
    promptTokens: breakdown.promptTokens,
    completionTokens: breakdown.completionTokens,
  }
})

const composerSheets = computed(() =>
  buildComposerSheets({
    mention:
      mentionActive.value && mentionCandidates.value.length > 0
        ? { candidates: mentionCandidates.value, activeIndex: mentionActiveIndex.value }
        : null,
    questions: activeQuestions.value,
    permissions: activeRequests.value,
    todos: harness.activeTodos,
    contextOpen: contextOpen.value,
    context: contextSheet.value,
  }),
)

const chatInputRef = ref<{ chooseMention: (candidate: MentionCandidate) => void } | null>(null)

function handleMentionMirror(
  open: boolean,
  query: string,
  candidates: MentionCandidate[],
  index: number,
): void {
  void query
  mentionCandidates.value = open ? candidates : []
  mentionActiveIndex.value = open ? index : 0
  mentionActive.value = open && candidates.length > 0
}

function handleMentionSelect(candidate: MentionCandidate): void {
  chatInputRef.value?.chooseMention(candidate)
}

function handleMentionHover(index: number): void {
  mentionActiveIndex.value = index
}

function handleToggleContext(): void {
  contextOpen.value = !contextOpen.value
}

function handleCloseContext(): void {
  contextOpen.value = false
}

function handleContextMetrics(
  metrics: Pick<ContextSheetState, 'used' | 'limit' | 'percent'>,
): void {
  contextMetrics.value = metrics
}

const isSubagentSession = computed(() => Boolean(activeSession.value?.parent_id))

const inputDisabled = computed(() => !props.canPrompt || activeSession.value?.status === 'busy')
const inputStoppable = computed(() =>
  Boolean(props.canPrompt && activeSession.value?.status === 'busy'),
)
const busyMessage = computed(() => {
  if (!props.canPrompt) return 'Workspace is not ready for prompts.'
  if (activeSession.value?.status === 'busy')
    return 'Agent is running — stop or wait to send another message.'
  return undefined
})

const cleanupFns: Array<() => void> = []

function setupSocketListeners(): void {
  subscribeToWorkspace(props.workspaceId)

  cleanupFns.push(
    onEvent('harness.part_updated', (data) => {
      if (data.workspace_id === props.workspaceId) {
        harness.handlePartUpdated(data.session_id, data.delta, {
          step: data.step,
          partId: data.part_id,
        })
        if (data.delta?.patch || data.delta?.compaction) {
          void harness.fetchParts(data.session_id)
        }
      }
    }),
  )

  cleanupFns.push(
    onEvent('harness.todo_updated', (data) => {
      if (data.workspace_id === props.workspaceId) {
        harness.handleTodoUpdated(data.session_id, data.todos)
      }
    }),
  )

  cleanupFns.push(
    onEvent('harness.subtask_started', (data) => {
      if (data.workspace_id === props.workspaceId) {
        harness.handleSubtaskStarted(data.session_id, {
          subtask_id: data.subtask_id,
          agent: data.agent,
          description: data.description,
          part_id: data.part_id,
          child_session_id: data.child_session_id,
        })
        if (data.child_session_id) {
          void harness.fetchSessions(props.workspaceId)
          void harness.fetchParts(data.child_session_id)
        }
      }
    }),
  )

  cleanupFns.push(
    onEvent('harness.subtask_finished', (data) => {
      if (data.workspace_id === props.workspaceId) {
        harness.handleSubtaskFinished(data.session_id, {
          subtask_id: data.subtask_id,
          agent: data.agent,
          status: data.status,
          summary: data.summary,
          child_session_id: data.child_session_id,
        })
      }
    }),
  )

  cleanupFns.push(
    onEvent('harness.session_status', (data) => {
      if (data.workspace_id === props.workspaceId) {
        harness.handleSessionStatus(data.session_id, data.status)
        if (data.status === 'idle') {
          void harness.fetchParts(data.session_id)
          void harness.fetchTodos(data.session_id)
        }
      }
    }),
  )

  cleanupFns.push(
    onEvent('harness.permission_required', (data) => {
      if (data.workspace_id === props.workspaceId) {
        if (!data.request_id) return
        if (data.decision) {
          harness.handlePermissionResolved(data.request_id, data.decision)
          return
        }
        harness.handlePermissionRequired({
          request_id: data.request_id,
          session_id: data.session_id,
          workspace_id: data.workspace_id,
          tool: data.tool,
          pattern: data.pattern,
          title: data.title,
          call_id: data.call_id,
        })
      }
    }),
  )

  cleanupFns.push(
    onEvent('harness.question_required', (data) => {
      if (data.workspace_id === props.workspaceId) {
        if (!data.request_id) return
        harness.handleQuestionRequired({
          request_id: data.request_id,
          session_id: data.session_id,
          workspace_id: data.workspace_id,
          questions: data.questions ?? [],
          call_id: data.call_id,
          status: data.status === 'pending' ? 'pending' : undefined,
        })
      }
    }),
  )
}

function cleanupSocket(): void {
  unsubscribeFromWorkspace(props.workspaceId)
  for (const fn of cleanupFns.splice(0)) fn()
}

onMounted(() => {
  setupSocketListeners()
  void harness.fetchSessions(props.workspaceId).then(() => applySessionQuery())
  void skillStore.fetchSkills()
})

onUnmounted(() => {
  cleanupSocket()
})

watch(
  () => props.workspaceId,
  (next, prev) => {
    if (next === prev) return
    cleanupSocket()
    setupSocketListeners()
    void harness.fetchSessions(next).then(() => applySessionQuery())
  },
)

function applySessionQuery(): void {
  const sessionId = route.query.session
  if (typeof sessionId !== 'string' || !sessionId) return
  if (harness.sessions.some((session) => session.id === sessionId)) {
    harness.setActiveSession(sessionId)
  }
}

watch(
  () => route.query.session,
  () => {
    applySessionQuery()
  },
  { immediate: true },
)

watch(
  () => harness.sessions,
  () => {
    applySessionQuery()
  },
)

watch(
  () => harness.activeSessionId,
  (sessionId) => {
    if (!sessionId) {
      composerMode.value = 'build'
      return
    }
    void harness.fetchParts(sessionId)
    void harness.fetchTodos(sessionId)
    const session = harness.sessions.find((item) => item.id === sessionId)
    if (session) composerMode.value = session.mode
  },
  { immediate: true },
)

watch(composerMode, async (mode, prev) => {
  if (!harness.activeSessionId || mode === prev) return
  const session = harness.activeSession
  if (!session || session.status === 'busy' || session.mode === mode) return
  await harness.updateSessionMode(harness.activeSessionId, mode)
})

async function handleSend(
  prompt: string,
  mode: HarnessSessionMode,
  model: string,
  skillIds: string[],
  effort: string,
): Promise<void> {
  if (isSubagentSession.value) return
  sending.value = true
  try {
    if (!harness.activeSessionId) {
      await harness.createSession(props.workspaceId, prompt, mode, model, skillIds, effort)
    } else {
      await harness.sendMessage(harness.activeSessionId, prompt, {
        mode,
        model,
        skillIds,
        reasoningEffort: effort,
      })
    }
  } finally {
    sending.value = false
  }
}

async function handleStop(): Promise<void> {
  if (!harness.activeSessionId) return
  await harness.abortSession(harness.activeSessionId)
}

async function handleResolve(
  requestId: string,
  response: 'once' | 'always' | 'reject',
): Promise<void> {
  const request = activeRequests.value.find((item) => item.request_id === requestId)
  if (!request) return
  resolving.value = true
  try {
    await harness.resolvePermission(request.session_id, request.request_id, response)
  } finally {
    resolving.value = false
  }
}

async function handleQuestionSubmit(requestId: string, answers: string[]): Promise<void> {
  const request = activeQuestions.value.find((item) => item.request_id === requestId)
  if (!request) return
  answeringQuestion.value = true
  try {
    await harness.resolveQuestion(request.session_id, request.request_id, answers)
  } finally {
    answeringQuestion.value = false
  }
}

async function handleQuestionSkip(requestId: string): Promise<void> {
  const request = activeQuestions.value.find((item) => item.request_id === requestId)
  if (!request) return
  answeringQuestion.value = true
  try {
    await harness.resolveQuestion(request.session_id, request.request_id, [], true)
  } finally {
    answeringQuestion.value = false
  }
}

function handleOpenSubtask(childSessionId: string): void {
  if (harness.sessions.some((session) => session.id === childSessionId)) {
    harness.setActiveSession(childSessionId)
  } else {
    void harness.fetchSessions(props.workspaceId).then(() => {
      if (harness.sessions.some((session) => session.id === childSessionId)) {
        harness.setActiveSession(childSessionId)
      }
    })
  }
}

function handleTerminalButtonClick(): void {
  if (!props.canPrompt) return
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

function handleDesktopButtonClick(): void {
  if (!props.canPrompt) return
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

const terminalButtonTitle = computed(() => {
  if (!terminalStore.isOpen) return 'Open terminal'
  if (terminalStore.isMinimized) return 'Restore terminal'
  return 'Minimize terminal'
})

const desktopButtonTitle = computed(() => {
  if (!desktopStore.isOpen) return 'Open desktop'
  if (desktopStore.isMinimized) return 'Restore desktop'
  return 'Minimize desktop'
})
</script>

<template>
  <div class="flex h-full min-h-0 w-full flex-col overflow-x-hidden">
    <HarnessChatContainer
      :messages="harness.activeMessages"
      :loading="harness.loading"
      :streaming-session-id="streamingSessionId"
      :child-session-ids="childSessionIds"
      class="min-h-0 flex-1"
      @open-subtask="handleOpenSubtask"
    />
    <div
      v-if="!isSubagentSession"
      class="relative z-10 flex min-w-0 shrink-0 items-end gap-0 overflow-x-hidden"
    >
      <div class="flex min-w-0 flex-1 flex-col">
        <HarnessSheetStack
          :sheets="composerSheets"
          :question-submitting="answeringQuestion"
          :permission-resolving="resolving"
          @mention-select="handleMentionSelect"
          @mention-hover="handleMentionHover"
          @question-submit="handleQuestionSubmit"
          @question-skip="handleQuestionSkip"
          @resolve="handleResolve"
          @close-context="handleCloseContext"
        />
        <HarnessChatInput
          ref="chatInputRef"
          class="min-w-0"
          :attached="composerSheets.length > 0"
          :disabled="inputDisabled"
          :sending="sending"
          :stoppable="inputStoppable"
          :busy-message="busyMessage"
          :mode="composerMode"
          :model="harness.modelInput"
          :effort="harness.effortInput"
          :workspace-id="props.workspaceId"
          :session-id="harness.activeSessionId"
          :files="fileExplorer.tree"
          :skill-options="skillStore.skills"
          :context-used="contextUsed"
          :context-open="contextOpen"
          mention-controlled
          :mention-active-index="mentionActiveIndex"
          @update:mode="composerMode = $event"
          @update:model="harness.modelInput = $event"
          @update:effort="harness.effortInput = $event"
          @send="handleSend"
          @stop="handleStop"
          @toggle-context="handleToggleContext"
          @context-metrics="handleContextMetrics"
          @mention-change="
            (open, query, candidates, index) => handleMentionMirror(open, query, candidates, index)
          "
          @mention-select="handleMentionSelect"
        />
      </div>
      <template v-if="showWorkspaceToolbar">
        <Button
          variant="ghost"
          size="icon-sm"
          class="mb-2 shrink-0"
          :disabled="!canPrompt"
          :title="fileExplorer.isOpen ? 'Hide files' : 'Open file explorer'"
          @click="fileExplorer.toggle()"
        >
          <FolderTree :size="16" :class="fileExplorer.isOpen ? 'text-primary' : ''" />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          class="mb-2 mr-2 shrink-0"
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
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          class="mb-2 mr-2 shrink-0"
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
        </Button>
      </template>
    </div>
  </div>
</template>
