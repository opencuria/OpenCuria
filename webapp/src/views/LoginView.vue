<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { UiButton } from '@/components/ui'
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
  <!--
    Full-bleed glass login — decorative blobs sit behind the glass panel,
    the glass backdrop-filter blurs + saturates them (Vibrancy effect).
  -->
  <div
    class="min-h-screen flex items-center justify-center px-[var(--sp-3)]"
    style="
      background-color: var(--color-background);
      background-image:
        radial-gradient(ellipse 60% 55% at 15% 10%, oklch(0.78 0.10 280 / 0.45) 0%, transparent 65%),
        radial-gradient(ellipse 50% 45% at 85% 15%, oklch(0.75 0.09 220 / 0.40) 0%, transparent 65%),
        radial-gradient(ellipse 55% 50% at 50% 90%, oklch(0.80 0.08 190 / 0.35) 0%, transparent 65%),
        radial-gradient(ellipse 40% 40% at 75% 60%, oklch(0.76 0.10 310 / 0.30) 0%, transparent 55%);
    "
  >
    <!-- Glass login card -->
    <div
      class="w-full max-w-sm relative overflow-hidden"
      style="
        border-radius: var(--radius-xl);
        background: var(--glass-bg-strong);
        backdrop-filter: var(--glass-filter-strong);
        -webkit-backdrop-filter: var(--glass-filter-strong);
        border: 1px solid var(--glass-border);
        box-shadow: var(--glass-shadow-lg);
        will-change: backdrop-filter;
      "
    >
      <!-- Highlight layer (specular glint top-left) -->
      <div
        aria-hidden="true"
        class="absolute inset-0 pointer-events-none z-0"
        style="
          background: var(--glass-highlight);
          border-radius: inherit;
        "
      />

      <!-- Illumination layer (ambient colour bleed) -->
      <div
        aria-hidden="true"
        class="absolute inset-0 pointer-events-none z-0"
        style="
          background: var(--glass-illumination);
          border-radius: inherit;
        "
      />

      <!-- Content layer -->
      <div class="relative z-10 px-[var(--sp-5)] py-[var(--sp-6)]">

        <!-- Brand -->
        <div class="flex items-center gap-3 mb-[var(--sp-5)] justify-center">
          <OpenCuriaLogo class="h-11 w-auto" alt="OpenCuria" />
        </div>

        <!-- Headline -->
        <h2
          class="text-[20px] font-semibold text-fg mb-[var(--sp-4)] text-center"
          style="letter-spacing: var(--tracking-title); line-height: var(--lh-heading)"
        >
          Sign in
        </h2>

        <form @submit.prevent="handleLogin" class="space-y-[var(--sp-3)]">
          <!-- Email field -->
          <div>
            <label
              for="email"
              class="block text-[13px] font-medium text-fg mb-[6px]"
              style="letter-spacing: var(--tracking-body)"
            >Email</label>
            <input
              id="email"
              v-model="email"
              type="email"
              autocomplete="email"
              required
              placeholder="you@example.com"
              class="w-full px-[var(--sp-3)] py-[10px] text-[15px] text-fg placeholder:text-muted-fg outline-none transition-[box-shadow,border-color] duration-[200ms]"
              style="
                border-radius: var(--radius-sm);
                background: oklch(1.00 0.000 0 / 0.06);
                border: 1px solid var(--glass-border);
                letter-spacing: var(--tracking-body);
                backdrop-filter: blur(4px);
                [transition-timing-function:var(--spring-snappy)];
              "
              @focus="($el as HTMLInputElement).style.boxShadow = '0 0 0 3px oklch(0.55 0.20 258 / 0.22), inset 0 1px 0 oklch(1 0 0 / 0.12)'; ($el as HTMLInputElement).style.borderColor = 'var(--color-primary)'"
              @blur="($el as HTMLInputElement).style.boxShadow = ''; ($el as HTMLInputElement).style.borderColor = ''"
            />
          </div>

          <!-- Password field -->
          <div>
            <label
              for="password"
              class="block text-[13px] font-medium text-fg mb-[6px]"
              style="letter-spacing: var(--tracking-body)"
            >Password</label>
            <input
              id="password"
              v-model="password"
              type="password"
              autocomplete="current-password"
              required
              placeholder="Enter your password"
              class="w-full px-[var(--sp-3)] py-[10px] text-[15px] text-fg placeholder:text-muted-fg outline-none transition-[box-shadow,border-color] duration-[200ms]"
              style="
                border-radius: var(--radius-sm);
                background: oklch(1.00 0.000 0 / 0.06);
                border: 1px solid var(--glass-border);
                letter-spacing: var(--tracking-body);
                backdrop-filter: blur(4px);
                [transition-timing-function:var(--spring-snappy)];
              "
              @focus="($el as HTMLInputElement).style.boxShadow = '0 0 0 3px oklch(0.55 0.20 258 / 0.22), inset 0 1px 0 oklch(1 0 0 / 0.12)'; ($el as HTMLInputElement).style.borderColor = 'var(--color-primary)'"
              @blur="($el as HTMLInputElement).style.boxShadow = ''; ($el as HTMLInputElement).style.borderColor = ''"
            />
          </div>

          <!-- Error message -->
          <p
            v-if="error"
            class="text-[13px] font-medium"
            style="color: var(--color-error); letter-spacing: var(--tracking-body)"
          >{{ error }}</p>

          <!-- Sign in button -->
          <button
            type="submit"
            :disabled="authStore.loading"
            class="w-full py-[11px] px-[var(--sp-3)] text-[15px] font-semibold text-primary-fg mt-[var(--sp-2)] cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
            style="
              border-radius: var(--radius-sm);
              background: var(--color-primary);
              border: 1px solid oklch(0.55 0.20 258 / 0.30);
              box-shadow:
                0 2px 10px oklch(0.55 0.20 258 / 0.40),
                inset 0 1px 0 oklch(1 0 0 / 0.22),
                inset 0 -1px 0 oklch(0 0 0 / 0.12);
              letter-spacing: var(--tracking-body);
              transition: transform 200ms var(--spring-snappy), box-shadow 200ms var(--spring-snappy), opacity 150ms;
            "
            @mousedown="($el as HTMLButtonElement).style.transform = 'scale(0.97)'"
            @mouseup="($el as HTMLButtonElement).style.transform = ''"
            @mouseleave="($el as HTMLButtonElement).style.transform = ''"
          >
            {{ authStore.loading ? 'Signing in…' : 'Sign in' }}
          </button>
        </form>

        <div v-if="ssoProvider" class="mt-[var(--sp-3)]">
          <UiButton
            variant="outline"
            class="w-full"
            :disabled="authStore.loading"
            @click="startSsoLogin"
          >
            Sign in with {{ ssoProvider.provider === 'keycloak' ? 'Keycloak' : 'SSO' }}
          </UiButton>
        </div>
      </div>
    </div>
  </div>
</template>
