<script setup lang="ts">
/**
 * WorkspaceChatHeader — minimaler Chat-Header (OpenWebUI-Navbar angelehnt).
 *
 * Kein Balken: transparent, ohne Border. Links steht der Workspace-Name
 * (dort per Klick/Pencil inline editierbar) mit Status + aktivem Chat als
 * dezenter Subline. Rechts: kompakte Panel-Toggles + `…`-Menü.
 * Die Chatliste lebt ausschließlich in der globalen Sidebar (gruppiert nach
 * Workspace); Chat-Rename/Delete passiert dort.
 * Start/Stop des Workspace hängen als Power-Button rechts (gleiche Logik wie
 * WorkspaceActions).
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

    <!-- Left: workspace name (editable here) + subtle status/chat subline -->
    <div class="flex min-w-0 flex-1 flex-col justify-center">
      <div class="flex min-w-0 items-center gap-1">
        <template v-if="editingWorkspace">
          <Input
            v-model="workspaceNameInput"
            class="h-7 min-w-0 flex-1 text-sm"
            maxlength="255"
            placeholder="Workspace name"
            data-testid="workspace-chat-header-name-input"
            @keydown.enter.prevent="saveWorkspaceRename"
            @keydown.esc.prevent="cancelWorkspaceRename"
          />
          <Button
            variant="ghost"
            size="icon-sm"
            class="h-7 w-7 shrink-0"
            title="Save workspace name"
            data-testid="workspace-chat-header-name-save"
            @click="saveWorkspaceRename"
          >
            <Check :size="14" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            class="h-7 w-7 shrink-0"
            title="Cancel"
            data-testid="workspace-chat-header-name-cancel"
            @click="cancelWorkspaceRename"
          >
            <X :size="14" />
          </Button>
        </template>
        <template v-else>
          <button
            type="button"
            class="min-w-0 flex-1 truncate py-0.5 text-left text-[15px] font-normal text-foreground transition-colors hover:text-foreground/80"
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
            class="hidden shrink-0 rounded p-1 text-muted-foreground opacity-0 transition-all hover:bg-muted hover:text-foreground focus-visible:opacity-100 sm:block [div:hover>&]:opacity-100"
            title="Rename workspace"
            data-testid="workspace-chat-header-rename"
            @click="startWorkspaceRename"
          >
            <Pencil :size="12" />
          </button>
        </template>
      </div>
      <div
        class="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
        data-testid="workspace-chat-header-status"
      >
        <span class="size-1.5 shrink-0 rounded-full" :class="statusDotClass" aria-hidden="true" />
        <Loader2 v-if="transitionLabel" :size="11" class="shrink-0 animate-spin" />
        <span class="shrink-0 truncate">{{ statusText }}</span>
        <span v-if="activeChatTitle" class="min-w-0 truncate" data-testid="workspace-chat-header-chat-title">
          · {{ activeChatTitle }}
        </span>
      </div>
    </div>

    <!-- Right: power + compact toggles + overflow menu -->
    <div class="flex shrink-0 items-center gap-0.5">
      <Button
        v-if="showStopButton || showStartButton"
        variant="ghost"
        size="icon-sm"
        :title="powerTitle"
        :disabled="powerDisabled"
        :data-testid="showStopButton ? 'workspace-chat-header-stop' : 'workspace-chat-header-start'"
        @click="handlePowerClick"
      >
        <Loader2 v-if="isTransitioning" :size="16" class="animate-spin" />
        <Square v-else-if="showStopButton" :size="16" />
        <Play v-else :size="16" />
      </Button>
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
            class="relative text-xs"
            data-testid="workspace-chat-header-toggle-processes"
            @select="emit('toggle-processes')"
          >
            <Container :size="14" :class="processesActive ? 'text-primary' : ''" />
            Background processes
            <span
              v-if="runningProcessCount > 0"
              class="ml-auto rounded-full bg-secondary px-1.5 text-[10px] text-secondary-foreground"
            >
              {{ runningProcessCount }}
            </span>
            <Check v-else-if="processesActive" :size="14" class="ml-auto text-primary" />
          </DropdownMenuItem>
          <DropdownMenuSeparator />
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
