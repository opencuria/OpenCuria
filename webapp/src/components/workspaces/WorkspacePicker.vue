<script setup lang="ts">
/**
 * WorkspacePicker — small pill shown ABOVE the home composer.
 *
 * Displays the selected workspace (name + status dot) and opens a dropdown
 * with search, a list of all non-deleted workspaces, and a footer action.
 * Default/localStorage selection logic lives in ChatHomeView; the picker
 * only displays, lets the user choose, and reports newly created workspaces.
 */
import { computed, ref } from 'vue'
import { ChevronDown, Container, Plus } from '@lucide/vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import { WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import CreateWorkspaceDialog from './CreateWorkspaceDialog.vue'
import { Input } from '@/components/ui/input'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

const selectedId = defineModel<string | null>({ default: null })

withDefaults(
  defineProps<{
    compact?: boolean
  }>(),
  {
    compact: false,
  },
)

const workspaceStore = useWorkspaceStore()
const searchQuery = ref('')
const createOpen = ref(false)

const HIDDEN_STATUSES = new Set<string>([
  WorkspaceStatus.REMOVED,
  WorkspaceStatus.DELETED,
  WorkspaceStatus.DELETING,
  WorkspaceStatus.PENDING_DELETION,
])

const selectableWorkspaces = computed<Workspace[]>(() =>
  workspaceStore.workspaces.filter((workspace) => !HIDDEN_STATUSES.has(workspace.status)),
)

const selectedWorkspace = computed<Workspace | null>(
  () => selectableWorkspaces.value.find((workspace) => workspace.id === selectedId.value) ?? null,
)

function isWorkspaceReady(workspace: Workspace): boolean {
  return (
    workspace.status === WorkspaceStatus.RUNNING &&
    workspace.runner_online &&
    !workspace.active_operation
  )
}

const isReady = computed(() =>
  selectedWorkspace.value ? isWorkspaceReady(selectedWorkspace.value) : false,
)

const filteredWorkspaces = computed<Workspace[]>(() => {
  const query = searchQuery.value.trim().toLowerCase()
  if (!query) return selectableWorkspaces.value
  return selectableWorkspaces.value.filter((workspace) =>
    workspace.name.toLowerCase().includes(query),
  )
})

function isWorkspaceBusy(workspace: Workspace): boolean {
  return Boolean(workspace.active_operation || workspace.has_active_session)
}

function statusDotClass(workspace: Workspace): string {
  if (isWorkspaceBusy(workspace)) return 'bg-amber-500'
  return isWorkspaceReady(workspace) ? 'bg-green-500' : 'bg-muted-foreground/40'
}

function statusLabel(workspace: Workspace): string {
  if (workspace.active_operation) {
    return workspaceStore.getWorkspaceTransitionLabel(workspace.id) ?? workspace.status
  }
  if (workspace.has_active_session) {
    return 'Busy'
  }
  if (workspace.status === WorkspaceStatus.RUNNING) {
    return workspace.runner_online ? 'Running' : 'Runner offline'
  }
  return workspace.status
}

function selectWorkspace(id: string): void {
  selectedId.value = id
  searchQuery.value = ''
}

function openCreate(): void {
  createOpen.value = true
}

function handleCreated(id: string | null): void {
  if (id) selectedId.value = id
}
</script>

<template>
  <div class="flex items-center justify-center gap-1.5">
    <DropdownMenu>
      <DropdownMenuTrigger as-child>
        <button
          type="button"
          aria-haspopup="listbox"
          :aria-label="
            selectedWorkspace
              ? `Workspace: ${selectedWorkspace.name}`
              : 'Select workspace'
          "
          data-testid="workspace-picker-trigger"
          class="inline-flex max-w-64 items-center gap-1.5 rounded-full border font-medium transition-colors focus-visible:outline-2 focus-visible:outline-primary"
          :class="[
            compact ? 'h-7 px-2.5 text-[11px]' : 'h-8 px-3 text-xs',
            isReady
              ? 'border-border bg-background text-foreground hover:bg-muted'
              : 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
          ]"
        >
          <Container :size="14" class="shrink-0" />
          <span class="truncate">{{ selectedWorkspace?.name ?? 'Select workspace' }}</span>
          <span
            v-if="selectedWorkspace"
            class="size-1.5 shrink-0 rounded-full"
            :class="statusDotClass(selectedWorkspace)"
            aria-hidden="true"
          />
          <ChevronDown :size="12" class="shrink-0 opacity-70" aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="center" class="w-72">
        <div class="p-1.5" @keydown.stop>
          <Input
            v-model="searchQuery"
            placeholder="Search workspaces..."
            aria-label="Search workspaces"
            class="h-8"
            data-testid="workspace-picker-search"
          />
        </div>
        <div role="listbox" aria-label="Workspaces" class="max-h-64 overflow-y-auto p-1">
          <DropdownMenuItem
            v-for="workspace in filteredWorkspaces"
            :key="workspace.id"
            role="option"
            :aria-selected="workspace.id === selectedId"
            class="gap-2"
            data-testid="workspace-picker-option"
            @click="selectWorkspace(workspace.id)"
          >
            <span
              class="size-1.5 shrink-0 rounded-full"
              :class="statusDotClass(workspace)"
              aria-hidden="true"
            />
            <span class="min-w-0 flex-1 truncate">{{ workspace.name }}</span>
            <span class="shrink-0 text-[11px] text-muted-foreground">
              {{ statusLabel(workspace) }}
            </span>
          </DropdownMenuItem>
          <p
            v-if="filteredWorkspaces.length === 0"
            class="px-3 py-4 text-center text-xs text-muted-foreground"
          >
            No workspaces found.
          </p>
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          class="gap-2 text-xs"
          data-testid="workspace-picker-create"
          @click="openCreate"
        >
          <Plus :size="14" aria-hidden="true" />
          Create workspace
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
    <button
      type="button"
      title="Create new workspace"
      aria-label="Create new workspace"
      data-testid="workspace-picker-new"
      class="inline-flex items-center gap-1 rounded-full text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary"
      :class="compact ? 'h-7 px-2 text-[11px]' : 'h-8 px-2.5'"
      @click="openCreate"
    >
      <Plus :size="14" aria-hidden="true" />
      New
    </button>
    <CreateWorkspaceDialog v-model:open="createOpen" @created="handleCreated">
      <span class="hidden" aria-hidden="true" />
    </CreateWorkspaceDialog>
  </div>
</template>
