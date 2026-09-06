<script setup lang="ts">
/**
 * ChatNavbar — schlanke Thread-Navbar (OpenWebUI-Navbar angelehnt).
 *
 * Links: Back + Chat-Titel (Inline-Rename via Harness) + Workspace-Pill.
 * Rechts: dezente Status-Zeile (Dot + text), Panel-Toggles, `…`-Menü mit
 * Destructive-Aktionen (Capture, Rename Workspace, Stop/Remove).
 */
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import type { HarnessSession } from '@/types/harness'
import type { WorkspaceDetail } from '@/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  ArrowLeft,
  Camera,
  Check,
  Container,
  Ellipsis,
  FolderTree,
  Loader2,
  MessageSquare,
  Monitor,
  Pencil,
  TerminalSquare,
  Trash2,
  X,
} from '@lucide/vue'

const props = defineProps<{
  workspace: WorkspaceDetail
  activeSession: HarnessSession | null
  hasHarnessChats: boolean
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
  renamingSession: boolean
}>()

const emit = defineEmits<{
  back: []
  'open-mobile-chats': []
  'rename-session': [sessionId: string, title: string]
  'save-workspace-name': [name: string]
  'toggle-files': []
  'toggle-terminal': []
  'toggle-desktop': []
  'toggle-processes': []
  'capture-image': []
  'delete-workspace': []
}>()

const editingSession = ref(false)
const sessionTitleInput = ref('')
const editingWorkspace = ref(false)
const workspaceNameInput = ref('')

const chatTitle = computed(
  () => props.activeSession?.title?.trim() || 'New chat',
)

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

function startSessionRename(): void {
  if (!props.activeSession) return
  sessionTitleInput.value = chatTitle.value
  editingSession.value = true
}

function cancelSessionRename(): void {
  editingSession.value = false
  sessionTitleInput.value = ''
}

function saveSessionRename(): void {
  if (!props.activeSession) {
    cancelSessionRename()
    return
  }
  const trimmed = sessionTitleInput.value.trim()
  if (!trimmed || trimmed === chatTitle.value) {
    cancelSessionRename()
    return
  }
  emit('rename-session', props.activeSession.id, trimmed)
  editingSession.value = false
  sessionTitleInput.value = ''
}

function startWorkspaceRename(): void {
  if (props.transitionLabel) return
  workspaceNameInput.value = props.workspace.name
  editingWorkspace.value = true
}

watch(
  () => props.workspace.id,
  () => {
    editingWorkspace.value = false
    editingSession.value = false
  },
)
</script>

<template>
  <header
    class="flex h-12 shrink-0 items-center gap-1 border-b border-border bg-header px-2 sm:px-3"
    data-testid="chat-navbar"
  >
    <!-- Left: back + chat title + workspace pill -->
    <div class="flex min-w-0 flex-1 items-center gap-1">
      <Button
        variant="ghost"
        size="icon-sm"
        class="shrink-0"
        title="Back"
        data-testid="chat-navbar-back"
        @click="emit('back')"
      >
        <ArrowLeft :size="16" />
      </Button>

      <div class="flex min-w-0 flex-1 flex-col justify-center">
        <div class="flex min-w-0 items-center gap-1">
          <template v-if="editingSession && activeSession">
            <Input
              v-model="sessionTitleInput"
              class="h-7 min-w-0 flex-1 text-sm"
              maxlength="255"
              data-testid="chat-navbar-title-input"
              @keydown.enter.prevent="saveSessionRename"
              @keydown.esc.prevent="cancelSessionRename"
            />
            <Button
              variant="ghost"
              size="icon-sm"
              class="h-7 w-7 shrink-0"
              title="Save chat title"
              :disabled="renamingSession"
              @click="saveSessionRename"
            >
              <Check :size="14" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              class="h-7 w-7 shrink-0"
              title="Cancel"
              @click="cancelSessionRename"
            >
              <X :size="14" />
            </Button>
          </template>
          <template v-else>
            <h1
              class="min-w-0 flex-1 truncate text-sm font-medium text-foreground"
              data-testid="chat-navbar-title"
            >
              {{ chatTitle }}
            </h1>
            <button
              v-if="activeSession"
              type="button"
              class="hidden shrink-0 rounded p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground sm:block"
              title="Rename chat"
              data-testid="chat-navbar-rename-chat"
              @click="startSessionRename"
            >
              <Pencil :size="12" />
            </button>
          </template>
        </div>
        <div class="flex min-w-0 items-center gap-1.5">
          <RouterLink
            :to="`/workspaces/${workspace.id}`"
            class="inline-flex min-w-0 items-center gap-1 rounded-full text-xs text-muted-foreground transition-colors hover:text-foreground"
            title="Workspace öffnen (alle Chats)"
            data-testid="chat-navbar-workspace-pill"
          >
            <Container :size="12" class="shrink-0" />
            <span class="truncate">{{ workspace.name }}</span>
          </RouterLink>
          <span
            class="hidden shrink-0 items-center gap-1 text-xs text-muted-foreground sm:inline-flex"
            data-testid="chat-navbar-status"
          >
            <span class="size-1.5 rounded-full" :class="statusDotClass" aria-hidden="true" />
            <Loader2 v-if="transitionLabel" :size="11" class="animate-spin" />
            <span class="truncate">{{ statusText }}</span>
          </span>
        </div>
      </div>
    </div>

    <!-- Right: toggles + overflow menu -->
    <div class="flex shrink-0 items-center gap-0.5">
      <Button
        v-if="hasHarnessChats"
        variant="ghost"
        size="icon-sm"
        class="md:hidden"
        title="Switch chat"
        data-testid="chat-navbar-mobile-chats"
        @click="emit('open-mobile-chats')"
      >
        <MessageSquare :size="16" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        :title="fileExplorerOpen ? 'Hide files' : 'Open file explorer'"
        data-testid="chat-navbar-toggle-files"
        @click="emit('toggle-files')"
      >
        <FolderTree :size="16" :class="fileExplorerOpen ? 'text-primary' : ''" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        :title="!terminalOpen ? 'Open terminal' : terminalMinimized ? 'Restore terminal' : 'Minimize terminal'"
        data-testid="chat-navbar-toggle-terminal"
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
        data-testid="chat-navbar-toggle-desktop"
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
            data-testid="chat-navbar-more"
          >
            <Ellipsis :size="16" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-56">
          <div v-if="editingWorkspace" class="flex items-center gap-1 p-1.5" @keydown.stop>
            <Input
              v-model="workspaceNameInput"
              class="h-8 min-w-0 flex-1 text-xs"
              maxlength="255"
              placeholder="Workspace name"
              data-testid="chat-navbar-workspace-name-input"
              @keydown.enter.prevent="emit('save-workspace-name', workspaceNameInput.trim())"
            />
            <Button
              variant="ghost"
              size="icon-sm"
              class="h-8 w-8 shrink-0"
              title="Save workspace name"
              @click="emit('save-workspace-name', workspaceNameInput.trim())"
            >
              <Check :size="14" />
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              class="h-8 w-8 shrink-0"
              title="Cancel"
              @click="editingWorkspace = false"
            >
              <X :size="14" />
            </Button>
          </div>
          <template v-else>
            <DropdownMenuItem
              class="text-xs"
              data-testid="chat-navbar-rename-workspace"
              :disabled="Boolean(transitionLabel)"
              @select="startWorkspaceRename"
            >
              <Pencil :size="14" />
              Rename workspace
            </DropdownMenuItem>
            <DropdownMenuItem
              class="relative text-xs"
              data-testid="chat-navbar-toggle-processes"
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
              data-testid="chat-navbar-capture-image"
              :disabled="!canPrompt"
              @select="emit('capture-image')"
            >
              <Camera :size="14" />
              Capture image
            </DropdownMenuItem>
            <DropdownMenuItem
              class="text-xs"
              variant="destructive"
              data-testid="chat-navbar-delete-workspace"
              @select="emit('delete-workspace')"
            >
              <Trash2 :size="14" />
              Delete workspace
            </DropdownMenuItem>
          </template>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </header>
</template>
