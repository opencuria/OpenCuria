/**
 * Credentials REST API service.
 */

import type {
  Credential,
  CredentialCreateIn,
  CredentialService,
  CredentialUpdateIn,
  PublicKeyOut,
} from '@/types'
import { get, post, patch, del } from './api'

export function listCredentialServices(): Promise<CredentialService[]> {
  return get<CredentialService[]>('/credential-services/')
}

export function listCredentials(): Promise<Credential[]> {
  return get<Credential[]>('/credentials/')
}

export function createCredential(data: CredentialCreateIn): Promise<Credential> {
  return post<Credential>('/credentials/', data)
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
