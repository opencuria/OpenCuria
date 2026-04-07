<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'

const router = useRouter()
const authStore = useAuthStore()

const name = ref('')
const error = ref('')

async function handleCreate() {
  error.value = ''

  if (!name.value.trim()) {
    error.value = 'Please provide an organization name.'
    return
  }

  const org = await authStore.createOrganization(name.value.trim())
  if (org) {
    router.push('/')
  } else {
    error.value = 'Failed to create organization. Please try a different name.'
  }
}
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-bg px-4">
    <div
      class="w-full max-w-md bg-surface rounded-[var(--radius-lg)] shadow-lg p-8 border border-border"
    >
      <!-- Brand -->
      <div class="flex items-center gap-3 mb-8 justify-center">
        <OpenCuriaLogo class="h-10 w-auto" alt="OpenCuria" />
      </div>

      <h2 class="text-lg font-semibold text-fg mb-2 text-center">Create your organization</h2>
      <p class="text-sm text-muted-fg text-center mb-6">
        You need an organization to start managing runners and workspaces.
      </p>

      <form @submit.prevent="handleCreate" class="space-y-4">
        <div>
          <label for="orgName" class="block text-sm font-medium text-fg mb-1.5">
            Organization name
          </label>
          <input
            id="orgName"
            v-model="name"
            type="text"
            required
            class="w-full px-3 py-2.5 bg-bg border border-border rounded-[var(--radius-md)] text-fg text-sm placeholder:text-muted-fg focus:outline-none focus:ring-2 focus:ring-primary/40 focus:border-primary transition-colors"
            placeholder="My Company"
          />
        </div>

        <!-- Error message -->
        <p v-if="error" class="text-sm text-error">{{ error }}</p>

        <button
          type="submit"
          :disabled="authStore.loading"
          class="w-full py-2.5 px-4 bg-primary text-primary-fg rounded-[var(--radius-md)] text-sm font-medium hover:bg-primary-hover transition-colors disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer"
        >
          {{ authStore.loading ? 'Creating...' : 'Create organization' }}
        </button>
      </form>
    </div>
  </div>
</template>
