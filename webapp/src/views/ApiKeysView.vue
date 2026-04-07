<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApiKeyStore } from '@/stores/apiKeys'
import ApiKeyCard from '@/components/api-keys/ApiKeyCard.vue'
import CreateApiKeyDialog from '@/components/api-keys/CreateApiKeyDialog.vue'
import RevokeApiKeyDialog from '@/components/api-keys/RevokeApiKeyDialog.vue'
import { UiSpinner, UiEmptyState } from '@/components/ui'
import type { APIKey } from '@/types'
import { KeyRound } from 'lucide-vue-next'

const apiKeyStore = useApiKeyStore()
const revokingKey = ref<APIKey | null>(null)

onMounted(() => {
  apiKeyStore.fetchKeys()
})

function onRevoke(key: APIKey): void {
  revokingKey.value = key
}

function onRevokeClose(): void {
  revokingKey.value = null
}
</script>

<template>
  <div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-fg">API Keys</h2>
        <p class="text-sm text-muted-fg mt-1">
          Long-lived keys for external integrations. Use
          <span class="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">Authorization: Bearer kai_…</span>
          or
          <span class="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">X-API-Key: kai_…</span>.
        </p>
      </div>
      <CreateApiKeyDialog />
    </div>

    <!-- Loading -->
    <div v-if="apiKeyStore.loading && !apiKeyStore.keys.length" class="flex justify-center py-12">
      <UiSpinner :size="24" />
    </div>

    <!-- Error -->
    <div
      v-else-if="apiKeyStore.error"
      class="rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ apiKeyStore.error }}
    </div>

    <!-- Empty state -->
    <UiEmptyState
      v-else-if="!apiKeyStore.keys.length"
      title="No API keys yet"
      description="Create your first API key to start integrating with external tools."
    >
      <template #icon>
        <KeyRound :size="32" />
      </template>
    </UiEmptyState>

    <!-- Key grid -->
    <div v-else class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
      <ApiKeyCard
        v-for="key in apiKeyStore.keys"
        :key="key.id"
        :api-key="key"
        @revoke="onRevoke"
      />
    </div>
  </div>

  <RevokeApiKeyDialog
    :api-key="revokingKey"
    @close="onRevokeClose"
  />
</template>
