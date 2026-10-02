/** Credential Pinia store: service catalog and real credentials. */

import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

import type { Credential, CredentialCreateIn, CredentialService, CredentialUpdateIn } from '@/types'
import * as credentialsApi from '@/services/credentials.api'
import { useAuthStore } from './auth'
import { useNotificationStore } from './notifications'

export const useCredentialStore = defineStore('credentials', () => {
  const credentials = ref<Credential[]>([])
  const services = ref<CredentialService[]>([])
  const servicesLoaded = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const servicesError = ref<string | null>(null)
  let credentialRequestId = 0
  let serviceRequestId = 0

  watch(
    () => useAuthStore().activeOrganizationId,
    () => {
      credentialRequestId += 1
      serviceRequestId += 1
      credentials.value = []
      services.value = []
      servicesLoaded.value = false
      error.value = null
      servicesError.value = null
      loading.value = false
    },
  )

  async function fetchCredentials(): Promise<void> {
    const authStore = useAuthStore()
    const organizationId = authStore.activeOrganizationId
    const requestId = ++credentialRequestId
    loading.value = true
    error.value = null
    try {
      const result = await credentialsApi.listCredentials()
      if (authStore.activeOrganizationId === organizationId && requestId === credentialRequestId)
        credentials.value = result
    } catch (e: unknown) {
      if (authStore.activeOrganizationId === organizationId && requestId === credentialRequestId) {
        error.value = e instanceof Error ? e.message : 'Failed to load credentials'
      }
    } finally {
      if (requestId === credentialRequestId) loading.value = false
    }
  }

  async function fetchServices(): Promise<void> {
    const authStore = useAuthStore()
    const organizationId = authStore.activeOrganizationId
    const requestId = ++serviceRequestId
    try {
      const result = await credentialsApi.listCredentialServices()
      if (authStore.activeOrganizationId !== organizationId || requestId !== serviceRequestId)
        return
      services.value = result
      servicesLoaded.value = true
      servicesError.value = null
    } catch (e: unknown) {
      if (authStore.activeOrganizationId !== organizationId || requestId !== serviceRequestId)
        return
      servicesLoaded.value = true
      servicesError.value = e instanceof Error ? e.message : 'Failed to load credential services'
    }
  }

  async function createCredential(data: CredentialCreateIn): Promise<boolean> {
    try {
      await credentialsApi.createCredential(data)
      useNotificationStore().success('Credential created', 'The credential has been saved.')
      await fetchCredentials()
      return true
    } catch (e: unknown) {
      useNotificationStore().error(
        'Creation failed',
        e instanceof Error ? e.message : 'Failed to create credential',
      )
      return false
    }
  }

  async function connectOAuthService(
    serviceId: string,
    name: string | undefined,
    organizationCredential: boolean,
  ): Promise<boolean> {
    try {
      const service = services.value.find((entry) => entry.id === serviceId)
      if (!service || !service.is_active || service.credential_type !== 'mcp_oauth') {
        throw new Error(
          service
            ? `${service.name} is inactive or is not an OAuth service.`
            : 'Credential service is unavailable in this organization.',
        )
      }
      const result = await credentialsApi.connectOAuthService(serviceId, {
        name: name ?? '',
        organization_credential: organizationCredential,
      })
      window.location.assign(validateAuthorizationUrl(result.authorization_url))
      return true
    } catch (e: unknown) {
      useNotificationStore().error(
        'OAuth connection failed',
        e instanceof Error ? e.message : 'Could not start OAuth connection.',
      )
      return false
    }
  }

  async function reconnectOAuthCredential(credentialId: string, name?: string): Promise<boolean> {
    try {
      const result = await credentialsApi.reconnectOAuthCredential(credentialId, name)
      window.location.assign(validateAuthorizationUrl(result.authorization_url))
      return true
    } catch (e: unknown) {
      useNotificationStore().error(
        'OAuth reconnection failed',
        e instanceof Error ? e.message : 'Could not reconnect OAuth credential.',
      )
      return false
    }
  }

  async function disconnectOAuthCredential(credentialId: string): Promise<boolean> {
    try {
      await credentialsApi.disconnectOAuthCredential(credentialId)
      await fetchCredentials()
      useNotificationStore().success(
        'OAuth disconnected',
        'The grant was disconnected. Workspace attachments are preserved.',
      )
      return true
    } catch (e: unknown) {
      useNotificationStore().error(
        'Disconnect failed',
        e instanceof Error ? e.message : 'Could not disconnect OAuth credential.',
      )
      return false
    }
  }

  async function getPublicKey(credentialId: string): Promise<string | null> {
    try {
      return (await credentialsApi.getPublicKey(credentialId)).public_key
    } catch (e: unknown) {
      useNotificationStore().error(
        'Error',
        e instanceof Error ? e.message : 'Failed to load public key',
      )
      return null
    }
  }

  async function updateCredential(id: string, data: CredentialUpdateIn): Promise<boolean> {
    try {
      await credentialsApi.updateCredential(id, data)
      useNotificationStore().success('Credential updated', 'The credential has been updated.')
      await fetchCredentials()
      return true
    } catch (e: unknown) {
      useNotificationStore().error(
        'Update failed',
        e instanceof Error ? e.message : 'Failed to update credential',
      )
      return false
    }
  }

  async function deleteCredential(id: string): Promise<boolean> {
    try {
      await credentialsApi.deleteCredential(id)
      useNotificationStore().success('Credential deleted', 'The credential has been removed.')
      await fetchCredentials()
      return true
    } catch (e: unknown) {
      useNotificationStore().error(
        'Deletion failed',
        e instanceof Error ? e.message : 'Failed to delete credential',
      )
      return false
    }
  }

  return {
    credentials,
    services,
    loading,
    error,
    servicesError,
    servicesLoaded,
    fetchCredentials,
    fetchServices,
    createCredential,
    connectOAuthService,
    reconnectOAuthCredential,
    disconnectOAuthCredential,
    getPublicKey,
    updateCredential,
    deleteCredential,
  }
})

/** OAuth authorization redirects are credential-free HTTPS URLs only. */
export function validateAuthorizationUrl(value: string): string {
  const url = new URL(value)
  if (url.protocol !== 'https:' || !url.hostname || url.username || url.password) {
    throw new Error('The OAuth provider returned an unsafe authorization URL.')
  }
  return url.toString()
}
