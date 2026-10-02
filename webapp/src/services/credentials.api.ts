/** Credentials and service REST APIs, including service-scoped OAuth actions. */

import type {
  Credential,
  CredentialCreateIn,
  CredentialService,
  CredentialServiceCreateIn,
  CredentialUpdateIn,
  OAuthConnectOut,
  PublicKeyOut,
} from '@/types'
import { del, get, patch, post } from './api'

export function listCredentialServices(): Promise<CredentialService[]> {
  return get<CredentialService[]>('/credential-services/')
}

export function listCredentials(): Promise<Credential[]> {
  return get<Credential[]>('/credentials/')
}

export function createCredential(data: CredentialCreateIn): Promise<Credential> {
  return post<Credential>('/credentials/', data)
}

export function connectOAuthService(
  serviceId: string,
  data: { name?: string; organization_credential: boolean },
): Promise<OAuthConnectOut> {
  return post<OAuthConnectOut>(
    `/credential-services/${encodeURIComponent(serviceId)}/oauth/connect/`,
    data,
    'include',
  )
}

export function reconnectOAuthCredential(
  credentialId: string,
  name?: string,
): Promise<OAuthConnectOut> {
  return post<OAuthConnectOut>(
    `/credentials/${encodeURIComponent(credentialId)}/oauth/reconnect/`,
    {
      name,
    },
    'include',
  )
}

export function disconnectOAuthCredential(credentialId: string): Promise<void> {
  return del<void>(`/credentials/${encodeURIComponent(credentialId)}/oauth/disconnect/`, 'include')
}

export function getPublicKey(credentialId: string): Promise<PublicKeyOut> {
  return get<PublicKeyOut>(`/credentials/${credentialId}/public-key/`)
}

export function updateCredential(id: string, data: CredentialUpdateIn): Promise<Credential> {
  return patch<Credential>(`/credentials/${id}/`, data)
}

export function deleteCredential(id: string): Promise<void> {
  return del<void>(`/credentials/${id}/`)
}

export function listOrganizationCredentialServices(): Promise<CredentialService[]> {
  return get<CredentialService[]>('/org-credential-services/')
}

export function createOrganizationCredentialService(
  data: CredentialServiceCreateIn,
): Promise<CredentialService> {
  return post<CredentialService>('/org-credential-services/', data)
}

export function toggleOrganizationCredentialService(
  serviceId: string,
  active: boolean,
): Promise<CredentialService> {
  return post<CredentialService>(
    `/org-credential-services/${encodeURIComponent(serviceId)}/activation/`,
    { active },
  )
}
