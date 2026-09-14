/**
 * Plugin Pinia store.
 *
 * Manages the org-visible plugin catalog (global + org-owned) and per
 * workspace plugin activations. Org-switch-safe: `clear()` empties the
 * state and `reload()` re-fetches from the active org (callers watch the
 * active organization id and call `reload`, same pattern the settings
 * panels use via `loadData()` on org change).
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type {
  Plugin,
  PluginCreateIn,
  PluginUpdateIn,
  WorkspacePlugin,
} from '@/types'
import { useAuthStore } from './auth'
import { useNotificationStore } from './notifications'
import * as pluginsApi from '@/services/plugins.api'

export const usePluginStore = defineStore('plugins', () => {
  // --- State ---
  const plugins = ref<Plugin[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const togglingIds = ref<string[]>([])
  /** Last org id the catalog was loaded for (org-switch safety). */
  const loadedOrgId = ref<string | null>(null)
  /** Monotonic request generation; stale org fetches are discarded. */
  let catalogRequestId = 0
  const workspacePluginsRequestId = ref<Record<string, number>>({})

  /** Workspace plugin lists keyed by workspace id. */
  const workspacePlugins = ref<Record<string, WorkspacePlugin[]>>({})
  const workspacePluginsLoading = ref<Record<string, boolean>>({})
  const workspacePluginsError = ref<Record<string, string | null>>({})

  // --- Getters ---
  const globalPlugins = computed(() => plugins.value.filter((p) => p.is_global))
  const orgPlugins = computed(() => plugins.value.filter((p) => !p.is_global))
  const orgEnabledPlugins = computed(() => plugins.value.filter((p) => p.org_enabled))

  // --- Actions ---

  /** Backwards-compatible single-toggle id (null unless exactly one toggle is in flight). */
  const togglingId = computed<string | null>(() =>
    togglingIds.value.length === 1 ? togglingIds.value[0]! : null,
  )

  function clear(): void {
    catalogRequestId += 1
    plugins.value = []
    error.value = null
    togglingIds.value = []
    loadedOrgId.value = null
    workspacePlugins.value = {}
    workspacePluginsLoading.value = {}
    workspacePluginsError.value = {}
    workspacePluginsRequestId.value = {}
  }

  /** Ensure catalog matches the active org; refetch on org switch. */
  async function reload(): Promise<void> {
    const authStore = useAuthStore()
    const orgId = authStore.activeOrganizationId
    if (!orgId) {
      clear()
      return
    }
    if (loadedOrgId.value !== orgId) {
      clear()
    }
    await fetchPlugins()
  }

  async function fetchPlugins(): Promise<void> {
    const authStore = useAuthStore()
    const orgId = authStore.activeOrganizationId
    const requestId = (catalogRequestId += 1)
    loading.value = true
    error.value = null
    try {
      const list = await pluginsApi.listPlugins()
      // Discard stale responses after an org switch / clear.
      if (requestId !== catalogRequestId) return
      if (authStore.activeOrganizationId !== orgId) return
      plugins.value = list
      loadedOrgId.value = orgId
    } catch (e: unknown) {
      if (requestId !== catalogRequestId) return
      if (authStore.activeOrganizationId !== orgId) return
      error.value = e instanceof Error ? e.message : 'Failed to load plugins'
    } finally {
      if (requestId === catalogRequestId && authStore.activeOrganizationId === orgId) {
        loading.value = false
      }
    }
  }

  async function createPlugin(data: PluginCreateIn): Promise<Plugin | null> {
    const notifications = useNotificationStore()
    try {
      const created = await pluginsApi.createPlugin(data)
      plugins.value = [...plugins.value, created].sort((a, b) =>
        a.name.localeCompare(b.name),
      )
      notifications.success('Plugin created', `"${created.name}" is ready.`)
      return created
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create plugin'
      notifications.error('Creation failed', msg)
      return null
    }
  }

  async function updatePlugin(id: string, data: PluginUpdateIn): Promise<Plugin | null> {
    const notifications = useNotificationStore()
    try {
      const updated = await pluginsApi.updatePlugin(id, data)
      const idx = plugins.value.findIndex((p) => p.id === id)
      if (idx !== -1) plugins.value[idx] = updated
      notifications.success('Plugin updated', `"${updated.name}" was saved.`)
      return updated
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update plugin'
      notifications.error('Update failed', msg)
      return null
    }
  }

  async function deletePlugin(id: string): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      await pluginsApi.deletePlugin(id)
      plugins.value = plugins.value.filter((p) => p.id !== id)
      notifications.success('Plugin deleted', 'The plugin was removed.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to delete plugin'
      notifications.error('Deletion failed', msg)
      return false
    }
  }

  async function toggleActivation(id: string, active: boolean): Promise<boolean> {
    const notifications = useNotificationStore()
    if (!togglingIds.value.includes(id)) togglingIds.value = [...togglingIds.value, id]
    try {
      const updated = await pluginsApi.togglePluginActivation(id, active)
      const idx = plugins.value.findIndex((p) => p.id === id)
      if (idx !== -1) plugins.value[idx] = updated
      notifications.success(
        active ? 'Plugin enabled' : 'Plugin disabled',
        `"${updated.name}" is now ${active ? 'enabled' : 'disabled'} for this organization.`,
      )
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update plugin activation'
      notifications.error('Update failed', msg)
      return false
    } finally {
      togglingIds.value = togglingIds.value.filter((entry) => entry !== id)
    }
  }

  async function fetchWorkspacePlugins(workspaceId: string): Promise<void> {
    const authStore = useAuthStore()
    const orgId = authStore.activeOrganizationId
    const requestId = (workspacePluginsRequestId.value[workspaceId] ?? 0) + 1
    workspacePluginsRequestId.value[workspaceId] = requestId
    workspacePluginsLoading.value[workspaceId] = true
    workspacePluginsError.value[workspaceId] = null
    try {
      const list = await pluginsApi.listWorkspacePlugins(workspaceId)
      // Discard stale responses after an org switch / clear.
      if (workspacePluginsRequestId.value[workspaceId] !== requestId) return
      if (authStore.activeOrganizationId !== orgId) return
      workspacePlugins.value[workspaceId] = list
    } catch (e: unknown) {
      if (workspacePluginsRequestId.value[workspaceId] !== requestId) return
      if (authStore.activeOrganizationId !== orgId) return
      workspacePluginsError.value[workspaceId] =
        e instanceof Error ? e.message : 'Failed to load workspace plugins'
    } finally {
      if (
        workspacePluginsRequestId.value[workspaceId] === requestId &&
        authStore.activeOrganizationId === orgId
      ) {
        workspacePluginsLoading.value[workspaceId] = false
      }
    }
  }

  async function setWorkspacePlugins(
    workspaceId: string,
    pluginIds: string[],
    opts: { notify?: boolean } = {},
  ): Promise<WorkspacePlugin[] | null> {
    const notifications = useNotificationStore()
    try {
      const list = await pluginsApi.updateWorkspacePlugins(workspaceId, {
        plugin_ids: pluginIds,
      })
      workspacePlugins.value[workspaceId] = list
      if (opts.notify !== false) {
        notifications.success('Workspace plugins updated', 'Plugin activations were saved.')
      }
      return list
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update workspace plugins'
      notifications.error('Update failed', msg)
      return null
    }
  }

  /** Re-fetch workspace plugins after a failed multi-step save (resync UI). */
  async function resyncWorkspacePlugins(workspaceId: string): Promise<void> {
    await fetchWorkspacePlugins(workspaceId)
  }

  return {
    // State
    plugins,
    loading,
    error,
    togglingIds,
    togglingId,
    loadedOrgId,
    workspacePlugins,
    workspacePluginsLoading,
    workspacePluginsError,
    // Getters
    globalPlugins,
    orgPlugins,
    orgEnabledPlugins,
    // Actions
    clear,
    reload,
    fetchPlugins,
    createPlugin,
    updatePlugin,
    deletePlugin,
    toggleActivation,
    fetchWorkspacePlugins,
    setWorkspacePlugins,
    resyncWorkspacePlugins,
  }
})
