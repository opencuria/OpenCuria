<!--
  ApiKeysPanel — API key list plus create/revoke dialogs.
-->
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApiKeyStore } from '@/stores/apiKeys'
import ApiKeyCard from '@/components/api-keys/ApiKeyCard.vue'
import CreateApiKeyDialog from '@/components/api-keys/CreateApiKeyDialog.vue'
import RevokeApiKeyDialog from '@/components/api-keys/RevokeApiKeyDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import SettingsSection from './SettingsSection.vue'
import type { APIKey } from '@/types'
import { KeyRound } from '@lucide/vue'

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
    <SettingsSection
      description="Long-lived keys for external integrations. Use Authorization: Bearer kai_… or X-API-Key: kai_…."
    >
      <template #actions>
        <CreateApiKeyDialog />
      </template>

      <div v-if="apiKeyStore.loading && !apiKeyStore.keys.length" class="flex justify-center py-12">
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="apiKeyStore.error"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        {{ apiKeyStore.error }}
      </div>

      <div
        v-else-if="!apiKeyStore.keys.length"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState
          :icon="KeyRound"
          title="No API keys yet"
          description="Create your first API key to start integrating with external tools."
        />
      </div>

      <div
        v-else
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <ApiKeyCard
          v-for="key in apiKeyStore.keys"
          :key="key.id"
          :api-key="key"
          @revoke="onRevoke"
        />
      </div>
    </SettingsSection>
  </div>

  <RevokeApiKeyDialog :api-key="revokingKey" @close="onRevokeClose" />
</template>
