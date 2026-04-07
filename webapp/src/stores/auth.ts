/**
 * Authentication Pinia store.
 *
 * Manages JWT tokens, user state, and organization context.
 * Token persistence via localStorage.
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

import type { User, UserOrg, TokenPair } from '@/types'
import * as authApi from '@/services/auth.api'
import * as orgApi from '@/services/organizations.api'
import { ApiRequestError } from '@/services/api'
import { useNotificationStore } from './notifications'

const ACCESS_TOKEN_KEY = 'kern_access_token'
const REFRESH_TOKEN_KEY = 'kern_refresh_token'
const ACTIVE_ORG_KEY = 'kern_active_org_id'
const USER_KEY = 'kern_user'
const ORGANIZATIONS_KEY = 'kern_organizations'

function loadStoredJson<T>(key: string): T | null {
  const rawValue = localStorage.getItem(key)
  if (!rawValue) {
    return null
  }

  try {
    return JSON.parse(rawValue) as T
  } catch {
    localStorage.removeItem(key)
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  // --- State ---
  const accessToken = ref<string | null>(localStorage.getItem(ACCESS_TOKEN_KEY))
  const refreshToken = ref<string | null>(localStorage.getItem(REFRESH_TOKEN_KEY))
  const user = ref<User | null>(loadStoredJson<User>(USER_KEY))
  const organizations = ref<UserOrg[]>(loadStoredJson<UserOrg[]>(ORGANIZATIONS_KEY) ?? [])
  const activeOrganizationId = ref<string | null>(localStorage.getItem(ACTIVE_ORG_KEY))
  const loading = ref(false)
  const initialized = ref(false)
  let initializePromise: Promise<void> | null = null

  // --- Getters ---
  const isAuthenticated = computed(() => !!accessToken.value)

  const activeOrganization = computed(() =>
    organizations.value.find((o) => o.id === activeOrganizationId.value) ?? null,
  )

  const isAdmin = computed(() => activeOrganization.value?.role === 'admin')

  const hasOrganizations = computed(() => organizations.value.length > 0)

  // --- Internal helpers ---

  function _saveTokens(tokens: TokenPair): void {
    accessToken.value = tokens.access_token
    refreshToken.value = tokens.refresh_token
    localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token)
    localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token)
  }

  function _saveProfile(userData: User | null, organizationData: UserOrg[]): void {
    user.value = userData
    organizations.value = organizationData

    if (userData) {
      localStorage.setItem(USER_KEY, JSON.stringify(userData))
    } else {
      localStorage.removeItem(USER_KEY)
    }

    localStorage.setItem(ORGANIZATIONS_KEY, JSON.stringify(organizationData))
  }

  function _clearAuth(): void {
    accessToken.value = null
    refreshToken.value = null
    _saveProfile(null, [])
    activeOrganizationId.value = null
    localStorage.removeItem(ACCESS_TOKEN_KEY)
    localStorage.removeItem(REFRESH_TOKEN_KEY)
    localStorage.removeItem(ACTIVE_ORG_KEY)
    localStorage.removeItem(USER_KEY)
    localStorage.removeItem(ORGANIZATIONS_KEY)
  }

  // --- Actions ---

  async function register(email: string, password: string): Promise<boolean> {
    const notifications = useNotificationStore()
    loading.value = true
    try {
      const tokens = await authApi.register({ email, password })
      _saveTokens(tokens)
      await fetchMe()
      notifications.success('Welcome!', 'Account created successfully.')
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Registration failed'
      notifications.error('Registration failed', msg)
      return false
    } finally {
      loading.value = false
    }
  }

  async function login(email: string, password: string): Promise<boolean> {
    const notifications = useNotificationStore()
    loading.value = true
    try {
      const tokens = await authApi.login({ email, password })
      _saveTokens(tokens)
      await fetchMe()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Login failed'
      notifications.error('Login failed', msg)
      return false
    } finally {
      loading.value = false
    }
  }

  async function loginWithTokens(tokens: TokenPair): Promise<boolean> {
    const notifications = useNotificationStore()
    loading.value = true
    try {
      _saveTokens(tokens)
      await fetchMe()
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Login failed'
      notifications.error('Login failed', msg)
      _clearAuth()
      return false
    } finally {
      loading.value = false
    }
  }

  function logout(): void {
    _clearAuth()
  }

  async function refreshAccessToken(): Promise<boolean> {
    if (!refreshToken.value) return false
    try {
      const tokens = await authApi.refresh({ refresh_token: refreshToken.value })
      _saveTokens(tokens)
      return true
    } catch {
      _clearAuth()
      return false
    }
  }

  async function fetchMe(): Promise<void> {
    try {
      const data = await authApi.getMe()
      const nextUser: User = {
        id: data.id,
        email: data.email,
        first_name: data.first_name,
        last_name: data.last_name,
      }
      _saveProfile(nextUser, data.organizations)

      // Sync token state from localStorage in case api.ts refreshed it
      const storedToken = localStorage.getItem(ACCESS_TOKEN_KEY)
      if (storedToken && storedToken !== accessToken.value) {
        accessToken.value = storedToken
      }
      const storedRefresh = localStorage.getItem(REFRESH_TOKEN_KEY)
      if (storedRefresh && storedRefresh !== refreshToken.value) {
        refreshToken.value = storedRefresh
      }

      // Set active org if not set or invalid
      if (
        !activeOrganizationId.value ||
        !data.organizations.find((o) => o.id === activeOrganizationId.value)
      ) {
        const firstOrganization = data.organizations[0]
        if (firstOrganization) {
          setActiveOrganization(firstOrganization.id)
        } else {
          activeOrganizationId.value = null
          localStorage.removeItem(ACTIVE_ORG_KEY)
        }
      }
    } catch (e) {
      // Only clear auth state on confirmed auth failures (401 after failed refresh).
      // api.ts already handles 401 by clearing localStorage and redirecting to /login.
      // For network errors or server errors (5xx), keep tokens so the user stays
      // logged in and can retry — don't force logout on transient failures.
      if (e instanceof ApiRequestError && e.status === 401) {
        _clearAuth()
      }
    }
  }

  function setActiveOrganization(orgId: string): void {
    activeOrganizationId.value = orgId
    localStorage.setItem(ACTIVE_ORG_KEY, orgId)
  }

  async function createOrganization(name: string): Promise<boolean> {
    const notifications = useNotificationStore()
    try {
      const org = await orgApi.createOrganization({ name })
      const nextOrganizations = [...organizations.value, {
        id: org.id,
        name: org.name,
        slug: org.slug,
        role: org.role,
        created_at: org.created_at,
      }]
      _saveProfile(user.value, nextOrganizations)
      setActiveOrganization(org.id)
      notifications.success('Organization created', `"${org.name}" is ready.`)
      return true
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : 'Failed to create organization'
      notifications.error('Creation failed', msg)
      return false
    }
  }

  /**
   * Returns true if the given JWT access token is expired or will expire
   * within the next 30 seconds (to avoid using a token that expires mid-request).
   */
  function _isTokenExpired(token: string): boolean {
    try {
      const parts = token.split('.')
      if (parts.length !== 3) return true
      const encodedPayload = parts[1]
      if (!encodedPayload) return true
      const payload = JSON.parse(atob(encodedPayload.replace(/-/g, '+').replace(/_/g, '/')))
      const exp = payload.exp
      if (!exp) return false
      return Date.now() / 1000 >= exp - 30
    } catch {
      return true
    }
  }

  /**
   * Initialize auth state on app start.
   * If we have a token, try to fetch the user profile.
   * Proactively refreshes the access token if it is expired or about to expire.
   */
  async function initialize(): Promise<void> {
    if (initialized.value) return
    if (initializePromise) {
      await initializePromise
      return
    }

    initializePromise = (async () => {
      if (accessToken.value) {
        // Proactively refresh if the access token is expired/expiring soon
        if (_isTokenExpired(accessToken.value)) {
          const refreshed = await refreshAccessToken()
          if (!refreshed) {
            // Both tokens are invalid — clear any stale state,
            // the router guard will redirect to /login
            _clearAuth()
            initialized.value = true
            return
          }
        }
        // fetchMe uses whatever token is in localStorage (possibly freshly refreshed)
        await fetchMe()
      }
      initialized.value = true
    })()

    try {
      await initializePromise
    } finally {
      initializePromise = null
    }
  }

  return {
    // State
    accessToken,
    refreshToken,
    user,
    organizations,
    activeOrganizationId,
    loading,
    initialized,
    // Getters
    isAuthenticated,
    activeOrganization,
    isAdmin,
    hasOrganizations,
    // Actions
    register,
    login,
    loginWithTokens,
    logout,
    refreshAccessToken,
    fetchMe,
    setActiveOrganization,
    createOrganization,
    initialize,
  }
})
