<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { useAuthStore } from '@/stores/auth'
import * as authApi from '@/services/auth.api'
import { connect as connectSocket } from '@/services/socket'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const error = ref('')

onMounted(async () => {
  const code = typeof route.query.code === 'string' ? route.query.code : ''
  const state = typeof route.query.state === 'string' ? route.query.state : ''
  const expectedState = sessionStorage.getItem('kern_sso_state')

  if (!code || !state || !expectedState || state !== expectedState) {
    error.value = 'Invalid SSO callback parameters.'
    setTimeout(() => router.replace('/login'), 1500)
    return
  }

  sessionStorage.removeItem('kern_sso_state')

  let success = false
  try {
    const redirectUri = `${window.location.origin}/sso/callback`
    const tokens = await authApi.exchangeSsoCode({ code, redirect_uri: redirectUri })
    success = await authStore.loginWithTokens(tokens)
  } catch {
    success = false
  }

  if (!success) {
    error.value = 'SSO sign-in failed.'
    setTimeout(() => router.replace('/login'), 1500)
    return
  }

  connectSocket()
  if (!authStore.hasOrganizations) {
    router.replace('/create-organization')
    return
  }
  router.replace('/')
})
</script>

<template>
  <div class="min-h-screen flex items-center justify-center px-4">
    <div class="flex flex-col items-center gap-3 text-center">
      <LoadingSpinner :size="32" />
      <p v-if="!error" class="text-sm text-muted-foreground">Signing in with SSO…</p>
      <p v-else class="text-sm text-destructive">{{ error }}</p>
    </div>
  </div>
</template>
