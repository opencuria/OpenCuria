<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuthStore } from '@/stores/auth'
import type { SsoProvider } from '@/types'
import * as authApi from '@/services/auth.api'
import { connect as connectSocket } from '@/services/socket'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'

const router = useRouter()
const authStore = useAuthStore()

const email = ref('')
const password = ref('')
const error = ref('')
const ssoProvider = ref<SsoProvider | null>(null)

async function handleLogin() {
  error.value = ''
  if (!email.value || !password.value) {
    error.value = 'Please enter your email and password.'
    return
  }

  const success = await authStore.login(email.value, password.value)
  if (success) {
    connectSocket()
    if (!authStore.hasOrganizations) {
      router.push('/create-organization')
    } else {
      router.push('/')
    }
  } else {
    error.value = 'Invalid email or password.'
  }
}

async function loadProviders() {
  const providers = await authApi.getAuthProviders()
  ssoProvider.value = providers.sso.enabled ? providers.sso : null
}

function buildRandomState(length = 32): string {
  const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
  let value = ''
  const random = crypto.getRandomValues(new Uint8Array(length))
  for (let i = 0; i < random.length; i += 1) {
    value += alphabet[random[i]! % alphabet.length]
  }
  return value
}

function startSsoLogin() {
  const provider = ssoProvider.value
  if (!provider?.authorization_endpoint || !provider.client_id) {
    error.value = 'SSO is currently unavailable.'
    return
  }

  const state = buildRandomState()
  sessionStorage.setItem('kern_sso_state', state)

  const redirectUri = `${window.location.origin}/sso/callback`
  const query = new URLSearchParams({
    client_id: provider.client_id,
    response_type: 'code',
    scope: provider.scope ?? 'openid email profile',
    redirect_uri: redirectUri,
    state,
  })
  window.location.href = `${provider.authorization_endpoint}?${query.toString()}`
}

onMounted(async () => {
  try {
    await loadProviders()
  } catch {
    ssoProvider.value = null
  }
})
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-background px-4">
    <Card class="w-full max-w-sm">
      <CardContent class="pt-6">
        <div class="flex items-center gap-3 mb-6 justify-center">
          <OpenCuriaLogo class="h-11 w-auto" alt="OpenCuria" />
        </div>

        <CardHeader class="px-0 pb-4 text-center">
          <CardTitle class="text-xl">Sign in</CardTitle>
        </CardHeader>

        <form @submit.prevent="handleLogin" class="space-y-4">
          <div class="space-y-2">
            <Label for="email">Email</Label>
            <Input
              id="email"
              v-model="email"
              type="email"
              autocomplete="email"
              required
              placeholder="you@example.com"
            />
          </div>

          <div class="space-y-2">
            <Label for="password">Password</Label>
            <Input
              id="password"
              v-model="password"
              type="password"
              autocomplete="current-password"
              required
              placeholder="Enter your password"
            />
          </div>

          <p v-if="error" class="text-sm text-destructive">{{ error }}</p>

          <Button type="submit" class="w-full" :disabled="authStore.loading">
            {{ authStore.loading ? 'Signing in…' : 'Sign in' }}
          </Button>
        </form>

        <div v-if="ssoProvider" class="mt-4">
          <Button
            variant="outline"
            class="w-full"
            :disabled="authStore.loading"
            @click="startSsoLogin"
          >
            Sign in with {{ ssoProvider.provider === 'keycloak' ? 'Keycloak' : 'SSO' }}
          </Button>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
