<script setup lang="ts">
/**
 * WorkspaceChatHeader — minimal chat header (inspired by the OpenWebUI navbar).
 *
 * No bar: transparent, no border. On the left the chat name is shown large,
 * below it the workspace name (inline editable via click/pencil) plus status.
 * Inside a subagent session the title becomes a breadcrumb (root › … › parent
 * › current) with a back-arrow to the direct parent. On the right: new chat,
 * background processes, `…` menu with start/stop, capture and delete, then
 * the side-panel toggle. The header only spans the chat area — with the side
 * panel open these buttons always sit to the left of the panel, whose own
 * tab bar (Git/Desktop/Terminal/Files) takes the full panel width. The chat
 * list lives exclusively in the global sidebar; chat rename/delete happens
 * there.
 */
import { computed, ref, watch } from 'vue'
import type { WorkspaceDetail } from '@/types'
import { WorkspaceStatus } from '@/types'
import type { HarnessSession } from '@/types/harness'
import { formatSubagentType } from '@/lib/harnessSubtaskActivity'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { SidebarTrigger } from '@/components/ui/sidebar'
import SidePanelToggle from '@/components/chat/SidePanelToggle.vue'
import {
  ArrowLeft,
  Camera,
  Check,
  ChevronRight,
  Container,
  Ellipsis,
  Loader2,
  Pencil,
  Play,
  Plus,
  Square,
  Trash2,
  X,
} from '@lucide/vue'

const props = defineProps<{
  workspace: WorkspaceDetail
  activeChatTitle: string | null
  transitionLabel: string | null
  autoStopLabel: string | null
  runnerOffline: boolean
  sidePanelOpen: boolean
  processesActive: boolean
  runningProcessCount: number
  canPrompt: boolean
  hasActiveSession?: boolean
  /** Play a subtle entrance animation (home → chat transition). */
  animateEntrance?: boolean
  /** Root → current session chain. Length > 1 means a subagent is open. */
  chatLineage?: HarnessSession[]
}>()

const emit = defineEmits<{
  'new-chat': []
  'start-workspace': []
  'stop-workspace': []
  'save-workspace-name': [name: string]
  'toggle-side-panel': []
  'toggle-processes': []
  'capture-image': []
  'delete-workspace': []
  'open-session': [sessionId: string]
}>()

const lineage = computed(() => props.chatLineage ?? [])
const currentSession = computed(() => lineage.value[lineage.value.length - 1] ?? null)
const parentSession = computed(() =>
  lineage.value.length >= 2 ? (lineage.value[lineage.value.length - 2] ?? null) : null,
)
const parentSessionId = computed(
  () => parentSession.value?.id ?? currentSession.value?.parent_id ?? null,
)
const showBreadcrumb = computed(() => lineage.value.length > 1)
const backToParentTitle = computed(() => {
  const title = parentSession.value?.title?.trim()
  if (title) return `Back to ${title}`
  return 'Back to parent chat'
})
const currentAgentLabel = computed(() => {
  if (!currentSession.value?.parent_id) return null
  return formatSubagentType(currentSession.value.agent_name)
})

type BreadcrumbCrumb =
  | { kind: 'session'; session: HarnessSession }
  | { kind: 'overflow'; sessions: HarnessSession[] }

const breadcrumbCrumbs = computed<BreadcrumbCrumb[]>(() => {
  const ancestors = lineage.value.slice(0, -1)
  if (ancestors.length <= 2) {
    return ancestors.map((session) => ({ kind: 'session' as const, session }))
  }
  return [
    { kind: 'session', session: ancestors[0]! },
    { kind: 'overflow', sessions: ancestors.slice(1, -1) },
    { kind: 'session', session: ancestors[ancestors.length - 1]! },
  ]
})

function sessionTitle(session: HarnessSession): string {
  return session.title?.trim() || 'Chat'
}

function crumbKey(crumb: BreadcrumbCrumb): string {
  return crumb.kind === 'session' ? crumb.session.id : 'overflow'
}

function openSession(sessionId: string): void {
  emit('open-session', sessionId)
}

function handleBackToParent(): void {
  if (!parentSessionId.value) return
  openSession(parentSessionId.value)
}

const editingWorkspace = ref(false)
const workspaceNameInput = ref('')

const statusDotClass = computed(() => {
  if (props.runnerOffline) return 'bg-muted-foreground/40'
  if (props.transitionLabel) return 'bg-amber-500'
  if (props.hasActiveSession) return 'bg-amber-500'
  return 'bg-green-500'
})

const statusText = computed(() => {
  if (props.runnerOffline) return 'Runner offline'
  if (props.transitionLabel) return props.transitionLabel
  if (props.autoStopLabel) return props.autoStopLabel
  return props.workspace.status
})

// Start/stop (same logic as WorkspaceActions): stop only for a running
// workspace with an online runner, start for a stopped workspace.
const isTransitioning = computed(() => props.transitionLabel !== null)
const showStopButton = computed(
  () => !props.runnerOffline && props.workspace.status === WorkspaceStatus.RUNNING,
)
const showStartButton = computed(
  () =>
    !showStopButton.value &&
    (props.runnerOffline || props.workspace.status === WorkspaceStatus.STOPPED),
)
const powerDisabled = computed(
  () => isTransitioning.value || (showStartButton.value && props.runnerOffline),
)
const powerTitle = computed(() => {
  if (isTransitioning.value) return props.transitionLabel ?? 'Workspace action in progress'
  if (showStopButton.value) return 'Stop workspace'
  if (props.runnerOffline) return 'Runner offline'
  return 'Start workspace'
})

function startWorkspaceRename(): void {
  if (props.transitionLabel) return
  workspaceNameInput.value = props.workspace.name
  editingWorkspace.value = true
}

function cancelWorkspaceRename(): void {
  editingWorkspace.value = false
  workspaceNameInput.value = ''
}

function saveWorkspaceRename(): void {
  const trimmed = workspaceNameInput.value.trim()
  editingWorkspace.value = false
  workspaceNameInput.value = ''
  if (!trimmed || trimmed === props.workspace.name) return
  emit('save-workspace-name', trimmed)
}

function handlePowerClick(): void {
  if (powerDisabled.value) return
  if (showStopButton.value) {
    emit('stop-workspace')
    return
  }
  emit('start-workspace')
}

watch(
  () => props.workspace.id,
  () => {
    editingWorkspace.value = false
    workspaceNameInput.value = ''
  },
)
</script>

<template>
  <header
    class="flex shrink-0 items-center gap-1 bg-transparent px-1.5 pb-1 pt-2 sm:px-2.5"
    data-testid="workspace-chat-header"
  >
    <SidebarTrigger class="shrink-0 text-muted-foreground" />

    <Button
      v-if="parentSessionId"
      variant="ghost"
      size="icon-sm"
      class="shrink-0"
      :title="backToParentTitle"
      :aria-label="backToParentTitle"
      data-testid="workspace-chat-header-back-to-parent"
      @click="handleBackToParent"
    >
      <ArrowLeft :size="16" />
    </Button>

    <!-- Left: chat name (large) or subagent breadcrumb + workspace name/status -->
    <div
      class="flex min-w-0 flex-1 flex-col justify-center"
      :class="animateEntrance ? 'chat-header-enter' : ''"
    >
      <div
        class="flex min-w-0 items-center gap-1.5 py-0.5"
        :data-testid="showBreadcrumb ? 'workspace-chat-header-breadcrumb' : undefined"
      >
        <template v-if="showBreadcrumb">
          <template v-for="crumb in breadcrumbCrumbs" :key="crumbKey(crumb)">
            <button
              v-if="crumb.kind === 'session'"
              type="button"
              class="min-w-0 max-w-[9rem] shrink truncate text-left text-[15px] font-normal text-muted-foreground transition-colors hover:text-foreground"
              :title="sessionTitle(crumb.session)"
              data-testid="workspace-chat-header-breadcrumb-item"
              :data-session-id="crumb.session.id"
              @click="openSession(crumb.session.id)"
            >
              {{ sessionTitle(crumb.session) }}
            </button>
            <DropdownMenu v-else>
              <DropdownMenuTrigger as-child>
                <button
                  type="button"
                  class="shrink-0 rounded px-1 text-[15px] leading-none text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  title="More sessions"
                  data-testid="workspace-chat-header-breadcrumb-overflow"
                >
                  …
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" class="w-56">
                <DropdownMenuItem
                  v-for="session in crumb.sessions"
                  :key="session.id"
                  class="text-xs"
                  data-testid="workspace-chat-header-breadcrumb-overflow-item"
                  :data-session-id="session.id"
                  @select="openSession(session.id)"
                >
                  {{ sessionTitle(session) }}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <ChevronRight
              :size="14"
              class="shrink-0 text-muted-foreground/50"
              aria-hidden="true"
            />
          </template>
        </template>
        <div
          class="min-w-0 truncate text-left text-[15px] font-normal text-foreground"
          data-testid="workspace-chat-header-chat-title"
        >
          {{ activeChatTitle || 'New chat' }}
        </div>
        <span
          v-if="currentAgentLabel"
          class="shrink-0 text-xs font-normal text-muted-foreground"
          data-testid="workspace-chat-header-agent-type"
        >
          {{ currentAgentLabel }}
        </span>
      </div>
      <div
        class="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
        data-testid="workspace-chat-header-status"
      >
        <template v-if="editingWorkspace">
          <Input
            v-model="workspaceNameInput"
            class="h-6 min-w-0 flex-1 text-xs"
            maxlength="255"
            placeholder="Workspace name"
            data-testid="workspace-chat-header-name-input"
            @keydown.enter.prevent="saveWorkspaceRename"
            @keydown.esc.prevent="cancelWorkspaceRename"
          />
          <Button
            variant="ghost"
            size="icon-sm"
            class="h-6 w-6 shrink-0"
            title="Save workspace name"
            data-testid="workspace-chat-header-name-save"
            @click="saveWorkspaceRename"
          >
            <Check :size="14" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            class="h-6 w-6 shrink-0"
            title="Cancel"
            data-testid="workspace-chat-header-name-cancel"
            @click="cancelWorkspaceRename"
          >
            <X :size="14" />
          </Button>
        </template>
        <template v-else>
          <span class="size-1.5 shrink-0 rounded-full" :class="statusDotClass" aria-hidden="true" />
          <Loader2 v-if="transitionLabel" :size="11" class="shrink-0 animate-spin" />
          <span class="shrink-0 truncate">{{ statusText }}</span>
          <span class="shrink-0">·</span>
          <button
            type="button"
            class="min-w-0 truncate text-left transition-colors hover:text-foreground"
            :class="transitionLabel ? 'cursor-default' : 'cursor-pointer'"
            title="Rename workspace"
            data-testid="workspace-chat-header-name"
            @click="startWorkspaceRename"
          >
            {{ workspace.name }}
          </button>
          <button
            v-if="!transitionLabel"
            type="button"
            class="hidden shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition-all hover:bg-muted hover:text-foreground focus-visible:opacity-100 sm:block [div:hover>&]:opacity-100"
            title="Rename workspace"
            data-testid="workspace-chat-header-rename"
            @click="startWorkspaceRename"
          >
            <Pencil :size="12" />
          </button>
        </template>
      </div>
    </div>

    <!-- Right: compact toggles + processes + overflow menu -->
    <div
      class="flex shrink-0 items-center gap-0.5"
      :class="animateEntrance ? 'chat-header-enter-stagger' : ''"
      data-testid="workspace-chat-header-actions"
    >
      <Button
        variant="ghost"
        size="icon-sm"
        title="New chat"
        data-testid="workspace-chat-header-new-chat"
        @click="emit('new-chat')"
      >
        <Plus :size="16" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        title="Background processes"
        data-testid="workspace-chat-header-toggle-processes"
        @click="emit('toggle-processes')"
      >
        <span class="relative inline-flex">
          <Container :size="16" :class="processesActive ? 'text-primary' : ''" />
          <span
            v-if="runningProcessCount > 0"
            class="absolute -bottom-1 -right-1 flex h-3 min-w-3 items-center justify-center rounded-full bg-secondary px-0.5 text-[9px] leading-none text-secondary-foreground"
          >
            {{ runningProcessCount }}
          </span>
          <span
            v-else-if="processesActive"
            class="absolute -bottom-1 -right-1 h-2 w-2 rounded-full bg-primary"
            title="Background processes open"
          />
        </span>
      </Button>

      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <Button
            variant="ghost"
            size="icon-sm"
            title="Workspace actions"
            data-testid="workspace-chat-header-more"
          >
            <Ellipsis :size="16" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-56">
          <DropdownMenuItem
            v-if="showStopButton || showStartButton"
            class="text-xs"
            :disabled="powerDisabled"
            :title="powerTitle"
            :data-testid="showStopButton ? 'workspace-chat-header-stop' : 'workspace-chat-header-start'"
            @select="handlePowerClick"
          >
            <Loader2 v-if="isTransitioning" :size="14" class="animate-spin" />
            <Square v-else-if="showStopButton" :size="14" />
            <Play v-else :size="14" />
            {{ showStopButton ? 'Stop workspace' : 'Start workspace' }}
          </DropdownMenuItem>
          <DropdownMenuSeparator v-if="showStopButton || showStartButton" />
          <DropdownMenuItem
            class="text-xs"
            data-testid="workspace-chat-header-capture-image"
            :disabled="!canPrompt"
            @select="emit('capture-image')"
          >
            <Camera :size="14" />
            Capture image
          </DropdownMenuItem>
          <DropdownMenuItem
            class="text-xs"
            variant="destructive"
            data-testid="workspace-chat-header-delete-workspace"
            @select="emit('delete-workspace')"
          >
            <Trash2 :size="14" />
            Delete workspace
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <SidePanelToggle :open="sidePanelOpen" @toggle="emit('toggle-side-panel')" />
    </div>
  </header>
</template>

<style scoped>
@media (prefers-reduced-motion: no-preference) {
  .chat-header-enter {
    animation: chat-header-in 200ms ease-out both;
  }

  .chat-header-enter-stagger > * {
    animation: chat-header-in 200ms ease-out both;
  }

  .chat-header-enter-stagger > *:nth-child(1) {
    animation-delay: 0ms;
  }

  .chat-header-enter-stagger > *:nth-child(2) {
    animation-delay: 40ms;
  }

  .chat-header-enter-stagger > *:nth-child(3) {
    animation-delay: 80ms;
  }

  .chat-header-enter-stagger > *:nth-child(4) {
    animation-delay: 120ms;
  }
}

@keyframes chat-header-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
