<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { isConnected } from '@/services/socket'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const authStore = useAuthStore()

const pageTitle = computed(() => {
  const meta = route.meta as { title?: string }
  return meta.title ?? 'Dashboard'
})
</script>

<template>
  <header
    class="flex items-center justify-between h-14 px-6 lg:px-8 border-b border-border bg-surface/80 backdrop-blur-sm shrink-0"
  >
    <div class="flex items-center gap-3">
      <!-- Mobile brand (visible on small screens) -->
      <div
        class="lg:hidden flex items-center justify-center w-8 h-8 rounded-[var(--radius-sm)] bg-primary text-primary-fg font-bold text-xs"
      >
        K
      </div>
      <h2 class="text-lg font-semibold text-fg">{{ pageTitle }}</h2>
    </div>

    <div class="flex items-center gap-3">
      <!-- Role badge -->
      <span
        v-if="authStore.activeOrganization"
        class="text-xs px-2 py-0.5 rounded-full border"
        :class="
          authStore.isAdmin
            ? 'bg-primary/10 text-primary border-primary/20'
            : 'bg-surface-hover text-muted-fg border-border'
        "
      >
        {{ authStore.isAdmin ? 'Admin' : 'Member' }}
      </span>

      <!-- Socket.IO connection status indicator -->
      <div
        class="flex items-center gap-2 text-xs text-muted-fg"
        :title="isConnected ? 'Real-time connected' : 'Real-time disconnected'"
      >
        <span
          :class="[
            'inline-block w-2 h-2 rounded-full',
            isConnected ? 'bg-success' : 'bg-error',
          ]"
        />
        {{ isConnected ? 'Live' : 'Offline' }}
      </div>
    </div>
  </header>
</template>
