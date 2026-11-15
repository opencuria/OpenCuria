/** Credentials and service REST APIs, including service-scoped OAuth actions. */

import { createCredentialServicesApi, createCredentialsApi } from '@opencuria/client/settings'
import type { OAuthConnectOut } from '@/types'
import { getApiClient } from './api'

export const {
  list: listCredentials,
  create: createCredential,
  update: updateCredential,
  delete: deleteCredential,
  publicKey: getPublicKey,
  disconnectOAuth: disconnectOAuthCredential,
  listServices: listCredentialServices,
} = createCredentialsApi(getApiClient)

export const {
  list: listOrganizationCredentialServices,
  create: createOrganizationCredentialService,
  setActive: toggleOrganizationCredentialService,
} = createCredentialServicesApi(getApiClient)

// Starting an OAuth flow sets its callback cookie, so these include credentials.

export function connectOAuthService(
  serviceId: string,
  data: { name?: string; organization_credential: boolean },
): Promise<OAuthConnectOut> {
  return getApiClient().request<OAuthConnectOut>(
    'POST',
    `/credential-services/${encodeURIComponent(serviceId)}/oauth/connect/`,
    data,
    undefined,
    'include',
  )
}

export function reconnectOAuthCredential(
  credentialId: string,
  name?: string,
): Promise<OAuthConnectOut> {
  return getApiClient().request<OAuthConnectOut>(
    'POST',
    `/credentials/${encodeURIComponent(credentialId)}/oauth/reconnect/`,
    { name },
    undefined,
    'include',
  )
}
