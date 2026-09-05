<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useHarnessStore } from '@/stores/harness'
import {
  onEvent,
  subscribeToWorkspace,
  unsubscribeFromWorkspace,
} from '@/services/socket'
import type { HarnessSessionMode } from '@/types/harness'
import HarnessChatContainer from '@/components/chat/HarnessChatContainer.vue'
import HarnessChatInput from '@/components/chat/HarnessChatInput.vue'
import HarnessPermissionDialog from '@/components/chat/HarnessPermissionDialog.vue'

const props = defineProps<{
  workspaceId: string
}>()

const harness = useHarnessStore()
const fileExplorer = useFileExplorerStore()
const sending = ref(false)
const resolving = ref(false)
const composerMode = ref<HarnessSessionMode>('build')

const activeSession = computed(() => harness.activeSession)
const streamingSessionId = computed(() =>
  activeSession.value?.status === 'busy' ? activeSession.value.id : null,
)

/** subtask_id -> child session id (parent/child link from child sessions). */
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
  return map
})

const activeRequest = computed(() => harness.activePermissionRequests[0] ?? null)

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
        })
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
        // `request_id` is present on interactive gates; runner-level
        // notifications without one are informational only.
        if (!data.request_id) return
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
}

function cleanupSocket(): void {
  unsubscribeFromWorkspace(props.workspaceId)
  for (const fn of cleanupFns.splice(0)) fn()
}

onMounted(() => {
  setupSocketListeners()
  void harness.fetchSessions(props.workspaceId)
})

onUnmounted(() => {
  cleanupSocket()
  harness.reset()
})

watch(
  () => props.workspaceId,
  (next, prev) => {
    if (next === prev) return
    cleanupSocket()
    harness.reset()
    setupSocketListeners()
    void harness.fetchSessions(next)
  },
)

watch(
  () => harness.activeSessionId,
  (sessionId) => {
    if (!sessionId) return
    void harness.fetchParts(sessionId)
    void harness.fetchTodos(sessionId)
  },
  { immediate: true },
)

async function handleSend(prompt: string, mode: HarnessSessionMode, model: string): Promise<void> {
  sending.value = true
  try {
    if (!harness.activeSessionId) {
      await harness.createSession(props.workspaceId, prompt, mode, model)
    } else {
      await harness.sendMessage(harness.activeSessionId, prompt)
    }
  } finally {
    sending.value = false
  }
}

async function handleStop(): Promise<void> {
  if (!harness.activeSessionId) return
  await harness.abortSession(harness.activeSessionId)
}

async function handleResolve(response: 'once' | 'always' | 'reject'): Promise<void> {
  const request = activeRequest.value
  if (!request) return
  resolving.value = true
  try {
    await harness.resolvePermission(request.session_id, request.request_id, response)
  } finally {
    resolving.value = false
  }
}

function handleOpenSubtask(childSessionId: string): void {
  if (harness.sessions.some((s) => s.id === childSessionId)) {
    harness.setActiveSession(childSessionId)
  }
}
</script>

<template>
  <div class="flex h-full min-h-0 w-full flex-col">
    <HarnessChatContainer
      :messages="harness.activeMessages"
      :todos="harness.activeTodos"
      :loading="harness.loading"
      :streaming-session-id="streamingSessionId"
      :child-session-ids="childSessionIds"
      class="min-h-0 flex-1"
      @open-subtask="handleOpenSubtask"
    />
    <HarnessChatInput
      :disabled="!activeSession || activeSession.status === 'busy'"
      :sending="sending"
      :stoppable="activeSession?.status === 'busy'"
      :mode="composerMode"
      :model="harness.modelInput"
      :workspace-id="props.workspaceId"
      :files="fileExplorer.tree"
      @update:mode="composerMode = $event"
      @update:model="harness.modelInput = $event"
      @send="handleSend"
      @stop="handleStop"
    />
    <HarnessPermissionDialog
      :request="activeRequest"
      :resolving="resolving"
      @resolve="handleResolve"
      @close="() => {}"
    />
  </div>
</template>
