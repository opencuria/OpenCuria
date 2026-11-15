/** Authentication and API-key REST API service. */

import { createApiKeysApi } from '@opencuria/client/settings'
import type {
  LoginRequest,
  TokenPair,
  RefreshRequest,
  SsoCallbackRequest,
  AuthProviders,
  UserWithOrgs,
} from '@/types'
import { get, getApiClient, post } from './api'

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

export const {
  list: listApiKeys,
  create: createApiKey,
  updatePermissions: updateApiKey,
  revoke: deleteApiKey,
  permissions: listApiKeyPermissions,
} = createApiKeysApi(getApiClient)
