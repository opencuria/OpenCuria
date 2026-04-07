/**
 * API Keys Pinia store.
 *
 * Manages long-lived API keys for external integrations.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import type { APIKey, APIKeyCreateIn, APIKeyCreatedOut, APIKeyPermissionInfo } from '@/types'
import * as authApi from '@/services/auth.api'
import { useNotificationStore } from './notifications'

export const useApiKeyStore = defineStore('apiKeys', () => {
  // --- State ---
  const keys = ref<APIKey[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const availablePermissions = ref<APIKeyPermissionInfo[]>([])

  // --- Actions ---

  async function fetchKeys(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      keys.value = await authApi.listApiKeys()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load API keys'
    } finally {
      loading.value = false
    }
  }

  async function fetchAvailablePermissions(): Promise<void> {
    try {
      availablePermissions.value = await authApi.listApiKeyPermissions()
    } catch {
      // non-critical — UI just won't show descriptions
    }
  }

  async function createKey(data: APIKeyCreateIn): Promise<APIKeyCreatedOut | null> {
    const notifications = useNotificationStore()
    try {
      const result = await authApi.createApiKey(data)
      await fetchKeys()
      return result
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create API key'
      notifications.error('Creation failed', msg)
      return null
    }
  }

  async function updateKeyPermissions(id: string, permissions: string[]): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      const updated = await authApi.updateApiKey(id, { permissions })
      const idx = keys.value.findIndex((k) => k.id === id)
      if (idx !== -1) keys.value[idx] = updated
      notifications.success('Permissions updated', 'API key permissions saved.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update permissions'
      notifications.error('Update failed', msg)
      return false
    }
  }

  async function revokeKey(id: string): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      await authApi.deleteApiKey(id)
      notifications.success('API key revoked', 'The key can no longer be used.')
      await fetchKeys()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to revoke API key'
      notifications.error('Revocation failed', msg)
      return false
    }
  }

  return {
    keys,
    loading,
    error,
    availablePermissions,
    fetchKeys,
    fetchAvailablePermissions,
    createKey,
    updateKeyPermissions,
    revokeKey,
  }
})
