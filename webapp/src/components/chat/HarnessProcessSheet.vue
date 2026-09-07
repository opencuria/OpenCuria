<script setup lang="ts">
import { computed, inject, ref } from 'vue'
import { Copy, RefreshCw, Square, X } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { harnessWorkspaceIdKey } from '@/lib/harnessWorkspaceContext'
import { useProcessesStore } from '@/stores/processes'
import { ProcessStatus } from '@/types'
import type { WorkspaceProcess } from '@/types'

const emit = defineEmits<{
  close: []
}>()

const workspaceIdRef = inject(harnessWorkspaceIdKey, ref(''))
const processesStore = useProcessesStore()
const copiedPath = ref<string | null>(null)

const workspaceId = computed(() => workspaceIdRef.value)
const processes = computed((): WorkspaceProcess[] =>
  processesStore.processesFor(workspaceId.value),
)
const loading = computed(() => processesStore.isLoading(workspaceId.value))
const error = computed(() => processesStore.errorFor(workspaceId.value))

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
  await processesStore.fetchProcesses(workspaceId.value)
}

async function handleStop(process: WorkspaceProcess): Promise<void> {
  await processesStore.stopProcess(workspaceId.value, process.id)
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
  <div class="px-4 py-3" data-testid="composer-process-sheet">
    <div class="flex items-center justify-between gap-2">
      <h3 class="text-sm font-medium text-foreground">Background processes</h3>
      <div class="flex items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="xs"
          :disabled="loading"
          title="Refresh processes"
          data-testid="composer-process-refresh"
          @click="handleRefresh"
        >
          <RefreshCw :size="14" :class="loading ? 'animate-spin' : ''" />
          Refresh
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          class="h-7 w-7 shrink-0 text-muted-foreground"
          title="Close processes"
          data-testid="composer-process-close"
          @click="emit('close')"
        >
          <X :size="14" />
        </Button>
      </div>
    </div>

    <p
      v-if="error && processes.length === 0"
      class="mt-3 text-sm text-destructive"
      data-testid="composer-process-error"
    >
      {{ error }}
    </p>
    <p
      v-else-if="processes.length === 0"
      class="mt-3 text-sm text-muted-foreground"
      data-testid="composer-process-empty"
    >
      No background processes yet.
    </p>
    <ul
      v-else
      class="mt-3 flex max-h-64 flex-col gap-2 overflow-y-auto"
      data-testid="composer-process-list"
    >
      <li
        v-for="process in processes"
        :key="process.id"
        class="flex flex-col gap-1 rounded-md border border-border px-3 py-2"
        data-testid="composer-process-row"
      >
        <div class="flex items-center gap-2">
          <span
            class="min-w-0 flex-1 truncate text-sm font-medium text-foreground"
            :title="process.name || process.id"
          >
            {{ displayName(process) }}
          </span>
          <Badge :variant="statusVariant(process.status)">{{ process.status }}</Badge>
          <Button
            v-if="isRunning(process)"
            type="button"
            variant="ghost"
            size="xs"
            :disabled="stopping(process.id)"
            title="Stop process"
            data-testid="composer-process-stop"
            @click="handleStop(process)"
          >
            <Square :size="13" />
            Stop
          </Button>
        </div>
        <p
          class="truncate font-mono text-xs text-muted-foreground"
          :title="process.command"
          data-testid="composer-process-command"
        >
          {{ process.command }}
        </p>
        <div class="flex min-w-0 items-center gap-1">
          <span
            v-if="process.log_path"
            class="min-w-0 truncate font-mono text-xs text-muted-foreground"
            :title="process.log_path"
            data-testid="composer-process-log-path"
          >
            {{ process.log_path }}
          </span>
          <span v-else class="text-xs text-muted-foreground">—</span>
          <button
            v-if="process.log_path"
            type="button"
            class="shrink-0 rounded p-0.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            :title="copiedPath === process.id ? 'Copied' : 'Copy log path'"
            data-testid="composer-process-copy"
            @click="handleCopyLogPath(process)"
          >
            <Copy :size="12" />
          </button>
        </div>
      </li>
    </ul>
  </div>
</template>
