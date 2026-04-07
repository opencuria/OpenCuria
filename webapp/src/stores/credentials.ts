/**
 * Credential Pinia store.
 *
 * Manages credential services (catalog) and org-scoped credentials.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import type { Credential, CredentialService, CredentialCreateIn, CredentialUpdateIn } from '@/types'
import * as credentialsApi from '@/services/credentials.api'
import { useNotificationStore } from './notifications'

export const useCredentialStore = defineStore('credentials', () => {
  // --- State ---
  const credentials = ref<Credential[]>([])
  const services = ref<CredentialService[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // --- Actions ---

  async function fetchCredentials(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      credentials.value = await credentialsApi.listCredentials()
    } catch (e: unknown) {
      error.value = e instanceof Error ? e.message : 'Failed to load credentials'
    } finally {
      loading.value = false
    }
  }

  async function fetchServices(): Promise<void> {
    try {
      services.value = await credentialsApi.listCredentialServices()
    } catch (e: unknown) {
      // Silently fail — services will be empty
      console.error('Failed to load credential services:', e)
    }
  }

  async function createCredential(data: CredentialCreateIn): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      await credentialsApi.createCredential(data)
      notifications.success('Credential created', 'The credential has been saved.')
      await fetchCredentials()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create credential'
      notifications.error('Creation failed', msg)
      return false
    }
  }

  async function getPublicKey(credentialId: string): Promise<string | null> {
    try {
      const result = await credentialsApi.getPublicKey(credentialId)
      return result.public_key
    } catch (e: unknown) {
      const notifications = useNotificationStore()
      const msg = e instanceof Error ? e.message : 'Failed to load public key'
      notifications.error('Error', msg)
      return null
    }
  }

  async function updateCredential(id: string, data: CredentialUpdateIn): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      await credentialsApi.updateCredential(id, data)
      notifications.success('Credential updated', 'The credential has been updated.')
      await fetchCredentials()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to update credential'
      notifications.error('Update failed', msg)
      return false
    }
  }

  async function deleteCredential(id: string): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      await credentialsApi.deleteCredential(id)
      notifications.success('Credential deleted', 'The credential has been removed.')
      await fetchCredentials()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to delete credential'
      notifications.error('Deletion failed', msg)
      return false
    }
  }

  return {
    // State
    credentials,
    services,
    loading,
    error,

    // Actions
    fetchCredentials,
    fetchServices,
    createCredential,
    getPublicKey,
    updateCredential,
    deleteCredential,
  }
})
