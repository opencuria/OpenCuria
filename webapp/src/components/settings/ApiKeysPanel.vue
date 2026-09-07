<!--
  ApiKeysPanel — Extrahierter API-Keys-Kern aus ApiKeysView (Schritt 5).

  Enthält Key-Grid + Create/Revoke-Dialoge ohne Page-Header.
  Wird vom Settings-Sheet (Tab "API Keys") und weiterhin von
  ApiKeysView wiederverwendet.
-->
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApiKeyStore } from '@/stores/apiKeys'
import ApiKeyCard from '@/components/api-keys/ApiKeyCard.vue'
import CreateApiKeyDialog from '@/components/api-keys/CreateApiKeyDialog.vue'
import RevokeApiKeyDialog from '@/components/api-keys/RevokeApiKeyDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Card, CardContent } from '@/components/ui/card'
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
  <div class="space-y-4">
    <div class="flex items-start justify-between gap-3">
      <p class="text-sm text-muted-foreground">
        Long-lived keys for external integrations. Use
        <span class="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">Authorization: Bearer kai_…</span>
        or
        <span class="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">X-API-Key: kai_…</span>.
      </p>
      <div class="shrink-0">
        <CreateApiKeyDialog />
      </div>
    </div>

    <!-- Loading -->
    <div v-if="apiKeyStore.loading && !apiKeyStore.keys.length" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <!-- Error -->
    <div
      v-else-if="apiKeyStore.error"
      class="rounded-md border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ apiKeyStore.error }}
    </div>

    <!-- Empty state -->
    <Card v-else-if="!apiKeyStore.keys.length">
      <CardContent class="flex flex-col items-center justify-center py-12 text-center">
        <KeyRound :size="32" class="text-muted-foreground mb-3" />
        <p class="text-sm font-medium text-foreground">No API keys yet</p>
        <p class="text-sm text-muted-foreground mt-1">
          Create your first API key to start integrating with external tools.
        </p>
      </CardContent>
    </Card>

    <!-- Key grid -->
    <div v-else class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
      <ApiKeyCard v-for="key in apiKeyStore.keys" :key="key.id" :api-key="key" @revoke="onRevoke" />
    </div>
  </div>

  <RevokeApiKeyDialog :api-key="revokingKey" @close="onRevokeClose" />
</template>
