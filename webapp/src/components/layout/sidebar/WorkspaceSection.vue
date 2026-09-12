<script setup lang="ts">
/**
 * Compact workspace list: live-first, max four rows, rest via /workspaces.
 */
import { Loader2, Plus } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { isLiveWorkspace, isOperatingWorkspace } from '@/lib/conversationGroups'
import type { Workspace } from '@/types'

const props = defineProps<{
  workspaces: Workspace[]
  totalCount: number
  activeWorkspaceId: string | null
}>()

const emit = defineEmits<{
  open: [workspaceId: string]
  'open-all': []
  create: []
}>()

function statusDotClass(workspace: Workspace): string {
  if (isOperatingWorkspace(workspace) || workspace.has_active_session) return 'bg-amber-500'
  if (isLiveWorkspace(workspace)) return 'bg-green-500'
  return 'bg-muted-foreground/40'
}
</script>

<template>
  <section data-testid="workspace-section" class="px-2 pt-2">
    <div class="flex h-7 items-center gap-1">
      <span class="text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
        Workspaces
      </span>
      <Button
        variant="ghost"
        size="icon-xs"
        class="ml-auto text-muted-foreground"
        data-testid="workspaces-create"
        aria-label="Manage workspaces"
        @click="emit('create')"
      >
        <Plus />
      </Button>
    </div>

    <div class="flex flex-col gap-0.5">
      <button
        v-for="workspace in props.workspaces"
        :key="workspace.id"
        type="button"
        data-testid="workspace-row"
        :aria-label="`Open workspace ${workspace.name}`"
        class="flex h-7 cursor-pointer items-center gap-1.5 rounded-xl px-2 text-left text-sm transition-colors focus-visible:outline-2 focus-visible:outline-primary"
        :class="
          props.activeWorkspaceId === workspace.id ? 'bg-primary/10' : 'hover:bg-muted'
        "
        @click="emit('open', workspace.id)"
      >
        <span class="size-1.5 shrink-0 rounded-full" :class="statusDotClass(workspace)" />
        <span class="min-w-0 flex-1 truncate text-[13px]">{{ workspace.name }}</span>
        <Loader2
          v-if="workspace.has_active_session"
          data-testid="workspace-busy"
          class="size-3 shrink-0 animate-spin text-primary"
        />
      </button>

      <button
        type="button"
        data-testid="all-workspaces"
        class="flex h-7 w-full items-center rounded-xl px-2 text-left text-[12px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-primary"
        @click="emit('open-all')"
      >
        All workspaces ({{ props.totalCount }})
      </button>
    </div>
  </section>
</template>
