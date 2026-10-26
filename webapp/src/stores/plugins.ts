/**
 * Plugin Pinia store.
 *
 * Manages the org-visible plugin catalog (global + org-owned).
 * Org-switch-safe: `clear()` empties the
 * state and `reload()` re-fetches from the active org (callers watch the
 * active organization id and call `reload`, same pattern the settings
 * panels use via `loadData()` on org change).
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import type { Plugin, PluginCreateIn, PluginUpdateIn } from '@/types'
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
      plugins.value = [...plugins.value, created].sort((a, b) => a.name.localeCompare(b.name))
      notifications.success('Plugin created', `"${created.name}" is ready.`)
      return created
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create plugin'
      notifications.error('Creation failed', msg)
      return null
    }
  }

  async function updatePlugin(id: string, data: PluginUpdateIn, options: { silent?: boolean } = {}): Promise<Plugin | null> {
    const notifications = useNotificationStore()
    try {
      const updated = await pluginsApi.updatePlugin(id, data)
      const idx = plugins.value.findIndex((p) => p.id === id)
      if (idx !== -1) plugins.value[idx] = updated
      if (!options.silent) notifications.success('Plugin updated', `"${updated.name}" was saved.`)
      return updated
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update plugin'
      if (!options.silent) notifications.error('Update failed', msg)
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

  return {
    // State
    plugins,
    loading,
    error,
    togglingIds,
    togglingId,
    loadedOrgId,
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
  }
})
