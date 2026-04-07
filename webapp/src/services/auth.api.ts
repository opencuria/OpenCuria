/**
 * Authentication REST API service.
 */

import type {
  LoginRequest,
  RegisterRequest,
  TokenPair,
  RefreshRequest,
  SsoCallbackRequest,
  AuthProviders,
  UserWithOrgs,
  APIKey,
  APIKeyCreateIn,
  APIKeyCreatedOut,
  APIKeyUpdateIn,
  APIKeyPermissionInfo,
} from '@/types'
import { get, post, del, patch } from './api'

export function register(data: RegisterRequest): Promise<TokenPair> {
  return post<TokenPair>('/auth/register/', data)
}

export function login(data: LoginRequest): Promise<TokenPair> {
  return post<TokenPair>('/auth/login/', data)
}

export function refresh(data: RefreshRequest): Promise<TokenPair> {
  return post<TokenPair>('/auth/refresh/', data)
}

export function getAuthProviders(): Promise<AuthProviders> {
  return get<AuthProviders>('/auth/providers/')
}

export function exchangeSsoCode(data: SsoCallbackRequest): Promise<TokenPair> {
  return post<TokenPair>('/auth/sso/callback/', data)
}

export function getMe(): Promise<UserWithOrgs> {
  return get<UserWithOrgs>('/auth/me/')
}

export function listApiKeys(): Promise<APIKey[]> {
  return get<APIKey[]>('/auth/api-keys/')
}

export function createApiKey(data: APIKeyCreateIn): Promise<APIKeyCreatedOut> {
  return post<APIKeyCreatedOut>('/auth/api-keys/', data)
}

export function updateApiKey(id: string, data: APIKeyUpdateIn): Promise<APIKey> {
  return patch<APIKey>(`/auth/api-keys/${id}/`, data)
}

export function deleteApiKey(id: string): Promise<void> {
  return del<void>(`/auth/api-keys/${id}/`)
}

export function listApiKeyPermissions(): Promise<APIKeyPermissionInfo[]> {
  return get<APIKeyPermissionInfo[]>('/auth/api-key-permissions/')
}
