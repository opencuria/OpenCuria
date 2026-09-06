/**
 * Background process store — workspace-bound processes started by the agent.
 *
 * Tracks process list per workspace (DB + live merge from the backend).
 * Live updates arrive via the `process:status_changed` socket event;
 * REST polling acts as fallback.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type {
  ProcessStatusChangedEvent,
  WorkspaceProcess,
} from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { useNotificationStore } from './notifications'

export const useProcessesStore = defineStore('processes', () => {
  // --- State ---
  const processesByWorkspace = ref<Record<string, WorkspaceProcess[]>>({})
  const loadingByWorkspace = ref<Record<string, boolean>>({})
  const errorByWorkspace = ref<Record<string, string | null>>({})
  const stoppingIds = ref<Set<string>>(new Set())

  // --- Getters ---

  function processesFor(workspaceId: string): WorkspaceProcess[] {
    return processesByWorkspace.value[workspaceId] ?? []
  }

  const runningCountFor = computed(() => {
    return (workspaceId: string): number =>
      processesFor(workspaceId).filter((p) => p.status === 'running').length
  })

  function isLoading(workspaceId: string): boolean {
    return loadingByWorkspace.value[workspaceId] ?? false
  }

  function errorFor(workspaceId: string): string | null {
    return errorByWorkspace.value[workspaceId] ?? null
  }

  function isStopping(processId: string): boolean {
    return stoppingIds.value.has(processId)
  }

  // --- Actions ---

  async function fetchProcesses(workspaceId: string): Promise<void> {
    loadingByWorkspace.value[workspaceId] = true
    errorByWorkspace.value[workspaceId] = null
    try {
      processesByWorkspace.value[workspaceId] = await workspacesApi.listProcesses(workspaceId)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to load processes'
      errorByWorkspace.value[workspaceId] = msg
    } finally {
      loadingByWorkspace.value[workspaceId] = false
    }
  }

  async function stopProcess(
    workspaceId: string,
    processId: string,
  ): Promise<boolean> {
    const notifications = useNotificationStore()
    const next = new Set(stoppingIds.value)
    next.add(processId)
    stoppingIds.value = next
    try {
      const updated = await workspacesApi.stopProcess(workspaceId, processId)
      upsertProcess(workspaceId, updated)
      notifications.success(
        'Process stopped',
        `Process ${updated.name || updated.id.slice(0, 8)} was stopped.`,
      )
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to stop process'
      notifications.error('Stop failed', msg)
      return false
    } finally {
      const done = new Set(stoppingIds.value)
      done.delete(processId)
      stoppingIds.value = done
    }
  }

  function upsertProcess(workspaceId: string, process: WorkspaceProcess): void {
    const list = processesByWorkspace.value[workspaceId] ?? []
    const idx = list.findIndex((p) => p.id === process.id)
    if (idx >= 0) {
      list[idx] = process
    } else {
      list.unshift(process)
    }
    processesByWorkspace.value[workspaceId] = [...list]
  }

  /**
   * Apply a `process:status_changed` push event.
   * Updates the matching entry in place; if unknown locally and the event
   * signals a running process, refetch the list to pick it up.
   */
  async function handleStatusChanged(event: ProcessStatusChangedEvent): Promise<void> {
    const list = processesByWorkspace.value[event.workspace_id]
    if (!list) return
    const idx = list.findIndex((p) => p.id === event.process_id)
    if (idx >= 0) {
      const current = list[idx]!
      list[idx] = {
        ...current,
        status: event.status,
        exit_code: event.exit_code,
        pid: event.pid,
        ended_at: event.status === 'running' ? current.ended_at : (current.ended_at ?? new Date().toISOString()),
        updated_at: new Date().toISOString(),
      }
      processesByWorkspace.value[event.workspace_id] = [...list]
      return
    }
    // Unknown process — refetch so newly started processes appear.
    if (event.status === 'running') {
      await fetchProcesses(event.workspace_id)
    }
  }

  function clearWorkspace(workspaceId: string): void {
    delete processesByWorkspace.value[workspaceId]
    delete loadingByWorkspace.value[workspaceId]
    delete errorByWorkspace.value[workspaceId]
  }

  function reset(): void {
    processesByWorkspace.value = {}
    loadingByWorkspace.value = {}
    errorByWorkspace.value = {}
    stoppingIds.value = new Set()
  }

  return {
    // State
    processesByWorkspace,
    loadingByWorkspace,
    errorByWorkspace,
    stoppingIds,
    // Getters
    processesFor,
    runningCountFor,
    isLoading,
    errorFor,
    isStopping,
    // Actions
    fetchProcesses,
    stopProcess,
    upsertProcess,
    handleStatusChanged,
    clearWorkspace,
    reset,
  }
})
