<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { isConnected } from '@/services/socket'
import { useAuthStore } from '@/stores/auth'
import { Badge } from '@/components/ui/badge'

const route = useRoute()
const authStore = useAuthStore()

const pageTitle = computed(() => {
  const meta = route.meta as { title?: string }
  return meta.title ?? 'Dashboard'
})
</script>

<template>
  <header class="flex h-14 shrink-0 items-center justify-between border-b bg-background/80 px-6 backdrop-blur-sm lg:px-8">
    <div class="flex items-center gap-3">
      <div class="flex size-8 items-center justify-center rounded-md bg-primary text-xs font-bold text-primary-foreground lg:hidden">
        K
      </div>
      <h2 class="text-lg font-semibold text-foreground">{{ pageTitle }}</h2>
    </div>

    <div class="flex items-center gap-3">
      <Badge v-if="authStore.activeOrganization" :variant="authStore.isAdmin ? 'default' : 'secondary'">
        {{ authStore.isAdmin ? 'Admin' : 'Member' }}
      </Badge>

      <div
        class="flex items-center gap-2 text-xs text-muted-foreground"
        :title="isConnected ? 'Real-time connected' : 'Real-time disconnected'"
      >
        <span
          class="inline-block size-2 rounded-full"
          :class="isConnected ? 'bg-green-500' : 'bg-destructive'"
        />
        {{ isConnected ? 'Live' : 'Offline' }}
      </div>
    </div>
  </header>
</template>
