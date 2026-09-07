<script setup lang="ts">
/**
 * WorkspaceChatHeader — minimaler Chat-Header (OpenWebUI-Navbar angelehnt).
 *
 * Kein Balken: transparent, ohne Border. Links steht der Chat-Name groß,
 * darunter Workspace-Name (per Klick/Pencil inline editierbar) plus Status.
 * Rechts: kompakte Panel-Toggles inkl. Background processes, dann `…`-Menü
 * mit Start/Stop, Capture und Delete. Die Chatliste lebt ausschließlich in
 * der globalen Sidebar; Chat-Rename/Delete passiert dort.
 */
import { computed, ref, watch } from 'vue'
import type { WorkspaceDetail } from '@/types'
import { WorkspaceStatus } from '@/types'
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
import {
  Camera,
  Check,
  Container,
  Ellipsis,
  FolderTree,
  Loader2,
  Monitor,
  Pencil,
  Play,
  Plus,
  Square,
  TerminalSquare,
  Trash2,
  X,
} from '@lucide/vue'

const props = defineProps<{
  workspace: WorkspaceDetail
  activeChatTitle: string | null
  transitionLabel: string | null
  autoStopLabel: string | null
  runnerOffline: boolean
  fileExplorerOpen: boolean
  terminalOpen: boolean
  terminalMinimized: boolean
  desktopOpen: boolean
  desktopMinimized: boolean
  processesActive: boolean
  runningProcessCount: number
  canPrompt: boolean
}>()

const emit = defineEmits<{
  'new-chat': []
  'start-workspace': []
  'stop-workspace': []
  'save-workspace-name': [name: string]
  'toggle-files': []
  'toggle-terminal': []
  'toggle-desktop': []
  'toggle-processes': []
  'capture-image': []
  'delete-workspace': []
}>()

const editingWorkspace = ref(false)
const workspaceNameInput = ref('')

const statusDotClass = computed(() => {
  if (props.runnerOffline) return 'bg-muted-foreground/40'
  if (props.transitionLabel) return 'bg-amber-500'
  return 'bg-green-500'
})

const statusText = computed(() => {
  if (props.runnerOffline) return 'Runner offline'
  if (props.transitionLabel) return props.transitionLabel
  if (props.autoStopLabel) return props.autoStopLabel
  return props.workspace.status
})

// Start/Stop (gleiche Logik wie WorkspaceActions): Stop nur bei laufendem
// Workspace mit online Runner, Start bei gestopptem Workspace.
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

    <!-- Left: chat name (large) + workspace name/status (small, editable) -->
    <div class="flex min-w-0 flex-1 flex-col justify-center">
      <div
        class="min-w-0 truncate py-0.5 text-left text-[15px] font-normal text-foreground"
        data-testid="workspace-chat-header-chat-title"
      >
        {{ activeChatTitle || 'New chat' }}
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
    <div class="flex shrink-0 items-center gap-0.5">
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
        :title="fileExplorerOpen ? 'Hide files' : 'Open file explorer'"
        data-testid="workspace-chat-header-toggle-files"
        @click="emit('toggle-files')"
      >
        <FolderTree :size="16" :class="fileExplorerOpen ? 'text-primary' : ''" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        :title="!terminalOpen ? 'Open terminal' : terminalMinimized ? 'Restore terminal' : 'Minimize terminal'"
        data-testid="workspace-chat-header-toggle-terminal"
        @click="emit('toggle-terminal')"
      >
        <span class="relative inline-flex">
          <TerminalSquare :size="16" :class="terminalOpen ? 'text-primary' : ''" />
          <span
            v-if="terminalOpen && terminalMinimized"
            class="absolute -bottom-1 -right-1 h-2 w-2 rounded-full bg-primary"
            title="Terminal minimized"
          />
        </span>
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        :title="!desktopOpen ? 'Open desktop' : desktopMinimized ? 'Restore desktop' : 'Minimize desktop'"
        data-testid="workspace-chat-header-toggle-desktop"
        @click="emit('toggle-desktop')"
      >
        <span class="relative inline-flex">
          <Monitor :size="16" :class="desktopOpen ? 'text-primary' : ''" />
          <span
            v-if="desktopOpen && desktopMinimized"
            class="absolute -bottom-1 -right-1 h-2 w-2 rounded-full bg-primary"
            title="Desktop minimized"
          />
        </span>
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
    </div>
  </header>
</template>
