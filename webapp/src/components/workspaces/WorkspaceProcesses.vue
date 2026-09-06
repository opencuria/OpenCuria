<script setup lang="ts">
/**
 * WorkspaceProcesses — background process list for a workspace.
 *
 * Read-only list (the agent starts processes); the user can stop or
 * force-stop running processes. Log content stays in the workspace —
 * only the log path is shown, with a copy button.
 */

import { computed, ref } from 'vue'
import { useProcessesStore } from '@/stores/processes'
import { ProcessStatus } from '@/types'
import type { WorkspaceProcess } from '@/types'
import { formatDate, formatRelativeTime } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Copy, RefreshCw, Square, OctagonX } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const processesStore = useProcessesStore()
const copiedPath = ref<string | null>(null)

const processes = computed((): WorkspaceProcess[] =>
  processesStore.processesFor(props.workspaceId),
)
const loading = computed(() => processesStore.isLoading(props.workspaceId))
const error = computed(() => processesStore.errorFor(props.workspaceId))

function statusVariant(
  status: WorkspaceProcess['status'],
): 'secondary' | 'outline' | 'destructive' | 'default' {
  switch (status) {
    case ProcessStatus.RUNNING:
      return 'secondary'
    case ProcessStatus.EXITED:
      return 'outline'
    case ProcessStatus.KILLED:
    case ProcessStatus.FAILED:
      return 'destructive'
    default:
      return 'outline'
  }
}

function isRunning(process: WorkspaceProcess): boolean {
  return process.status === ProcessStatus.RUNNING
}

function displayName(process: WorkspaceProcess): string {
  return process.name || process.id.slice(0, 8)
}

function stopping(processId: string): boolean {
  return processesStore.isStopping(processId)
}

async function handleRefresh(): Promise<void> {
  await processesStore.fetchProcesses(props.workspaceId)
}

async function handleStop(process: WorkspaceProcess, force: boolean): Promise<void> {
  await processesStore.stopProcess(props.workspaceId, process.id, force)
}

async function handleCopyLogPath(process: WorkspaceProcess): Promise<void> {
  if (!process.log_path) return
  try {
    await navigator.clipboard.writeText(process.log_path)
    copiedPath.value = process.id
    setTimeout(() => {
      if (copiedPath.value === process.id) copiedPath.value = null
    }, 1500)
  } catch {
    // Clipboard unavailable — the path is still visible for manual copy.
  }
}
</script>

<template>
  <div class="flex flex-col h-full min-h-0">
    <div class="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
      <div class="flex items-center gap-2 min-w-0">
        <span class="text-xs font-medium text-muted-foreground">Background processes</span>
        <Badge v-if="processes.length > 0" variant="outline">{{ processes.length }}</Badge>
      </div>
      <Button
        variant="ghost"
        size="xs"
        :disabled="loading"
        title="Refresh processes"
        @click="handleRefresh"
      >
        <RefreshCw :size="14" :class="loading ? 'animate-spin' : ''" />
        Refresh
      </Button>
    </div>

    <div v-if="loading && processes.length === 0" class="flex-1 flex items-center justify-center">
      <LoadingSpinner :size="20" />
    </div>

    <div v-else-if="error && processes.length === 0" class="flex-1 flex items-center justify-center p-4">
      <div class="text-center">
        <p class="text-sm text-destructive mb-2">{{ error }}</p>
        <Button size="sm" variant="outline" @click="handleRefresh">Retry</Button>
      </div>
    </div>

    <div v-else-if="processes.length === 0" class="flex-1 flex items-center justify-center p-4">
      <p class="text-sm text-muted-foreground">No background processes yet.</p>
    </div>

    <div v-else class="flex-1 min-h-0 overflow-auto">
      <table class="w-full text-sm">
        <thead class="sticky top-0 bg-muted/60 backdrop-blur">
          <tr class="text-left text-xs text-muted-foreground">
            <th class="px-3 py-2 font-medium">Name</th>
            <th class="px-3 py-2 font-medium hidden md:table-cell">Command</th>
            <th class="px-3 py-2 font-medium">Status</th>
            <th class="px-3 py-2 font-medium hidden sm:table-cell">PID</th>
            <th class="px-3 py-2 font-medium hidden lg:table-cell">Started</th>
            <th class="px-3 py-2 font-medium hidden sm:table-cell">Exit</th>
            <th class="px-3 py-2 font-medium hidden xl:table-cell">Log path</th>
            <th class="px-3 py-2 font-medium text-right">Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="process in processes"
            :key="process.id"
            class="border-t border-border hover:bg-muted/40"
          >
            <td class="px-3 py-2 font-medium max-w-40 truncate" :title="process.name || process.id">
              {{ displayName(process) }}
            </td>
            <td class="px-3 py-2 font-mono text-xs text-muted-foreground max-w-64 truncate hidden md:table-cell" :title="process.command">
              {{ process.command }}
            </td>
            <td class="px-3 py-2">
              <Badge :variant="statusVariant(process.status)">{{ process.status }}</Badge>
            </td>
            <td class="px-3 py-2 font-mono text-xs hidden sm:table-cell">
              {{ process.pid ?? '—' }}
            </td>
            <td class="px-3 py-2 text-xs text-muted-foreground whitespace-nowrap hidden lg:table-cell" :title="formatDate(process.started_at)">
              {{ formatRelativeTime(process.started_at) }}
            </td>
            <td class="px-3 py-2 font-mono text-xs hidden sm:table-cell">
              {{ process.exit_code ?? '—' }}
            </td>
            <td class="px-3 py-2 hidden xl:table-cell">
              <span v-if="process.log_path" class="flex items-center gap-1 min-w-0">
                <span class="font-mono text-xs text-muted-foreground truncate max-w-48" :title="process.log_path">
                  {{ process.log_path }}
                </span>
                <button
                  class="p-0.5 rounded hover:bg-muted text-muted-foreground hover:text-foreground transition-colors shrink-0"
                  :title="copiedPath === process.id ? 'Copied' : 'Copy log path'"
                  @click="handleCopyLogPath(process)"
                >
                  <Copy :size="12" />
                </button>
              </span>
              <span v-else class="text-xs text-muted-foreground">—</span>
            </td>
            <td class="px-3 py-2 text-right whitespace-nowrap">
              <template v-if="isRunning(process)">
                <Button
                  variant="ghost"
                  size="xs"
                  :disabled="stopping(process.id)"
                  title="Stop process (SIGTERM)"
                  @click="handleStop(process, false)"
                >
                  <Square :size="13" />
                  Stop
                </Button>
                <Button
                  variant="ghost"
                  size="xs"
                  class="text-destructive hover:text-destructive"
                  :disabled="stopping(process.id)"
                  title="Force-stop process (SIGKILL)"
                  @click="handleStop(process, true)"
                >
                  <OctagonX :size="13" />
                  Force
                </Button>
              </template>
              <span v-else class="text-xs text-muted-foreground">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
