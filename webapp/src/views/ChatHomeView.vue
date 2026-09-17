<script setup lang="ts">
/**
 * ChatHomeView — centered home screen (route `/`, name `home`).
 *
 * OpenWebUI-style placeholder layout: greeting + WorkspacePicker pill +
 * reused HarnessChatInput. Top right shows only the side-panel toggle
 * (no new chat / processes / overflow menu).
 * No polling: ChatSidebar handles live updates; a single
 * fetchWorkspaces()/fetchSkills() on mount is enough here.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Container, Plus } from '@lucide/vue'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import HarnessChatInput from '@/components/chat/HarnessChatInput.vue'
import SidePanelToggle from '@/components/chat/SidePanelToggle.vue'
import CreateWorkspaceDialog from '@/components/workspaces/CreateWorkspaceDialog.vue'
import WorkspacePicker from '@/components/workspaces/WorkspacePicker.vue'
import WorkspaceToolsSplit from '@/components/workspaces/WorkspaceToolsSplit.vue'
import { Button } from '@/components/ui/button'
import {
  HOME_EXIT_MS,
  armComposerTransition,
  prefersReducedMotion,
} from '@/lib/composerTransition'
import { getDroppedFiles, isFileDrag } from '@/lib/chatUpload'
import type { HarnessSessionMode } from '@/types/harness'
import { WorkspaceStatus } from '@/types'
import { useAuthStore } from '@/stores/auth'
import { useHarnessStore } from '@/stores/harness'
import { useSidePanelStore } from '@/stores/sidePanel'
import { useSkillStore } from '@/stores/skills'
import { useWorkspaceStore } from '@/stores/workspaces'

const LAST_WORKSPACE_KEY = 'opencuria:last-workspace'

const router = useRouter()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const sidePanelStore = useSidePanelStore()
const skillStore = useSkillStore()
const workspaceStore = useWorkspaceStore()

const selectedWorkspaceId = ref<string | null>(null)
const sending = ref(false)
const composerMode = ref<HarnessSessionMode>('build')
const createOpen = ref(false)
/** Drives the exit animation of the greeting block while sending. */
const leaving = ref(false)
const composerWrapRef = ref<HTMLElement | null>(null)
const homeChatInputRef = ref<{
  uploadChatFiles: (files: File[] | FileList) => Promise<void>
} | null>(null)

/**
 * Home-composer drop zone: same behaviour as the workspace chat panel —
 * files dropped anywhere over the home column upload into
 * `/workspace/.opencuria/user-uploaded` and insert an `@file:` token.
 * Subtle composer highlight only (no overlay); ignored while no workspace
 * is ready for prompts.
 */
const homeDragCounter = ref(0)
const homeDragActive = ref(false)
const homeUploadDragState = computed(() => ({
  active: homeDragActive.value,
  uploading: false,
}))

function resetHomeDrag(): void {
  homeDragCounter.value = 0
  homeDragActive.value = false
}

function onHomeDragEnter(event: DragEvent): void {
  if (!canPrompt.value || !isFileDrag(event)) return
  event.preventDefault()
  homeDragCounter.value += 1
  homeDragActive.value = true
}

function onHomeDragLeave(event: DragEvent): void {
  if (!homeDragActive.value) return
  event.preventDefault()
  homeDragCounter.value -= 1
  if (homeDragCounter.value <= 0) resetHomeDrag()
}

function onHomeDragOver(event: DragEvent): void {
  if (!canPrompt.value || !isFileDrag(event)) return
  event.preventDefault()
}

function onHomeDrop(event: DragEvent): void {
  // Same dedup guard as the workspace chat panel: the composer card handles
  // its own drops (stopPropagation + preventDefault), so a handled event
  // reaching us must not upload a second time.
  if (event.defaultPrevented) return
  const wasActive = homeDragActive.value
  resetHomeDrag()
  if (!canPrompt.value) return
  if (!wasActive && !isFileDrag(event)) return
  event.preventDefault()
  const files = getDroppedFiles(event)
  if (files.length === 0) return
  void homeChatInputRef.value?.uploadChatFiles(files)
}

function isWorkspaceAvailable(workspace: {
  status: WorkspaceStatus
  runner_online: boolean
  active_operation: string | null
  has_active_session: boolean
}): boolean {
  return (
    workspace.status === WorkspaceStatus.RUNNING &&
    workspace.runner_online &&
    !workspace.active_operation &&
    !workspace.has_active_session
  )
}

const greetingName = computed(() => {
  const email = authStore.user?.email ?? ''
  const prefix = email.split('@')[0]?.trim() ?? ''
  return prefix
})

const readyWorkspace = computed(() => {
  const workspace = workspaceStore.workspaces.find(
    (entry) => entry.id === selectedWorkspaceId.value,
  )
  if (
    workspace &&
    workspace.status === WorkspaceStatus.RUNNING &&
    workspace.runner_online &&
    !workspace.active_operation
  ) {
    return workspace
  }
  return null
})

const hasWorkspaces = computed(() => workspaceStore.workspaces.length > 0)

const busyMessage = computed(() => {
  if (!hasWorkspaces.value) return ''
  if (!selectedWorkspaceId.value) return 'Select a running workspace'
  const workspace = workspaceStore.workspaces.find(
    (entry) => entry.id === selectedWorkspaceId.value,
  )
  if (!workspace) return 'Select a running workspace'
  if (workspace.status !== WorkspaceStatus.RUNNING || !workspace.runner_online) {
    return 'Workspace is not ready — runner offline or stopped'
  }
  if (workspace.active_operation) {
    return workspaceStore.getWorkspaceTransitionLabel(workspace.id) ?? 'Workspace is busy…'
  }
  return ''
})

const inputDisabled = computed(() => sending.value || !readyWorkspace.value)
const canPrompt = computed(() => readyWorkspace.value !== null)

function handleToggleSidePanel(): void {
  if (!canPrompt.value) return
  sidePanelStore.toggle()
}

function pickInitialWorkspace(): void {
  const workspaces = workspaceStore.workspaces
  if (!workspaces.length) {
    selectedWorkspaceId.value = null
    return
  }
  const stored = localStorage.getItem(LAST_WORKSPACE_KEY)
  if (stored) {
    const storedWorkspace = workspaces.find((entry) => entry.id === stored)
    if (storedWorkspace && isWorkspaceAvailable(storedWorkspace)) {
      selectedWorkspaceId.value = stored
      return
    }
  }
  const firstAvailable = workspaces.find((entry) => isWorkspaceAvailable(entry))
  if (firstAvailable) {
    selectedWorkspaceId.value = firstAvailable.id
    return
  }
  const firstReady = workspaces.find(
    (entry) => entry.status === WorkspaceStatus.RUNNING && entry.runner_online,
  )
  selectedWorkspaceId.value = firstReady?.id ?? workspaces[0]?.id ?? null
}

function persistSelection(id: string | null): void {
  if (id) {
    localStorage.setItem(LAST_WORKSPACE_KEY, id)
  } else {
    localStorage.removeItem(LAST_WORKSPACE_KEY)
  }
}

function handleSelectionChange(id: string | null): void {
  selectedWorkspaceId.value = id
  persistSelection(id)
}

function handleCreatedWorkspace(id: string | null): void {
  if (!id) return
  void workspaceStore.fetchWorkspaces().then(() => {
    selectedWorkspaceId.value = id
    persistSelection(id)
  })
}

async function handleSend(
  prompt: string,
  mode: HarnessSessionMode,
  model: string,
  skillIds: string[],
  effort: string,
): Promise<void> {
  const workspace = readyWorkspace.value
  if (!workspace || sending.value) return
  sending.value = true
  const animate = !prefersReducedMotion()
  const sendStartedAt = performance.now()
  if (animate) leaving.value = true
  try {
    const session = await harnessStore.createSession(
      workspace.id,
      prompt,
      mode,
      model,
      skillIds,
      effort,
    )
    const sessionId = session?.id ?? harnessStore.activeSessionId
    if (!sessionId) {
      leaving.value = false
      return
    }
    if (animate) {
      // Hand the composer rect to the chat view so it can FLIP-morph the
      // composer from this centered position to the bottom of the chat.
      const card =
        composerWrapRef.value?.querySelector('[data-testid="composer-card"]') ??
        composerWrapRef.value
      const rect = card?.getBoundingClientRect()
      if (rect) armComposerTransition(rect)
      // Let the exit animation finish before unmounting (the network wait
      // usually covers it already).
      const remaining = HOME_EXIT_MS - (performance.now() - sendStartedAt)
      if (remaining > 0) {
        await new Promise((resolve) => setTimeout(resolve, remaining))
      }
    }
    // Seed the detail view so it renders immediately (no loading spinner);
    // the polling refresh merges fresh fields into it.
    workspaceStore.activeWorkspace = workspace
    await router.push({
      name: 'workspace-detail',
      params: { id: workspace.id },
      query: { session: sessionId },
    })
  } finally {
    sending.value = false
  }
}

onMounted(async () => {
  if (workspaceStore.workspaces.length === 0) {
    await workspaceStore.fetchWorkspaces()
  }
  pickInitialWorkspace()
  void skillStore.fetchSkills()
})
</script>

<template>
  <WorkspaceToolsSplit
    :workspace-id="selectedWorkspaceId ?? ''"
    :can-prompt="canPrompt"
  >
    <template #header>
      <header
        class="flex shrink-0 items-center justify-end px-1.5 pb-1 pt-2 sm:px-2.5"
        data-testid="chat-home-header"
      >
        <SidePanelToggle
          :open="sidePanelStore.isOpen"
          :disabled="!canPrompt"
          @toggle="handleToggleSidePanel"
        />
      </header>
    </template>

    <div
      class="flex min-h-0 flex-1 flex-col overflow-y-auto"
      data-testid="chat-home"
      @dragenter="onHomeDragEnter"
      @dragleave="onHomeDragLeave"
      @dragover="onHomeDragOver"
      @drop="onHomeDrop"
    >
      <div class="m-auto w-full max-w-3xl px-4 py-16 text-center sm:py-24">
        <div
          class="transition-all duration-200 ease-in"
          :class="leaving ? '-translate-y-2 opacity-0' : 'translate-y-0 opacity-100'"
          data-testid="chat-home-hero"
        >
          <div class="mb-4 flex justify-center">
            <OpenCuriaLogo icon-only motion="idle" alt="OpenCuria" class="size-16" />
          </div>

          <h1 class="text-2xl font-medium text-foreground" data-testid="chat-home-greeting">
            <template v-if="greetingName">How can I help, {{ greetingName }}?</template>
            <template v-else>How can I help?</template>
          </h1>

          <div class="mt-4 flex justify-center">
            <WorkspacePicker
              :model-value="selectedWorkspaceId"
              @update:model-value="handleSelectionChange"
            />
          </div>
        </div>

        <div
          v-if="!hasWorkspaces"
          class="mx-auto mt-6 flex max-w-md flex-col items-center gap-3 rounded-lg border border-border bg-card px-6 py-8"
          data-testid="chat-home-empty"
        >
          <Container :size="20" class="text-muted-foreground" aria-hidden="true" />
          <p class="text-sm text-muted-foreground">
            No workspace yet. Create a workspace to get started.
          </p>
          <Button size="sm" data-testid="chat-home-create" @click="createOpen = true">
            <Plus :size="14" aria-hidden="true" />
            Create workspace
          </Button>
          <CreateWorkspaceDialog v-model:open="createOpen" @created="handleCreatedWorkspace">
            <span class="hidden" aria-hidden="true" />
          </CreateWorkspaceDialog>
        </div>

        <div v-else ref="composerWrapRef" class="mt-6">
          <HarnessChatInput
            ref="homeChatInputRef"
            :workspace-id="selectedWorkspaceId ?? undefined"
            :session-id="null"
            :mode="composerMode"
            :model="harnessStore.modelInput"
            :effort="harnessStore.effortInput"
            :skill-options="skillStore.skills"
            :disabled="inputDisabled"
            :sending="sending"
            :busy-message="busyMessage"
            :upload-drag="homeUploadDragState"
            class="text-left"
            data-testid="chat-home-composer"
            @update:mode="composerMode = $event"
            @update:model="harnessStore.setComposerModel($event)"
            @update:effort="harnessStore.setComposerEffort($event)"
            @send="handleSend"
          />
        </div>
      </div>
    </div>
  </WorkspaceToolsSplit>
</template>
