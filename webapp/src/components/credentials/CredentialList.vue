<script setup lang="ts">
import type { Credential } from '@/types'
import CredentialCard from './CredentialCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { KeyRound } from '@lucide/vue'

const props = defineProps<{ credentials: Credential[] }>()

const emit = defineEmits<{
  edit: [credential: Credential]
  delete: [credential: Credential]
  viewPublicKey: [credential: Credential]
}>()
</script>

<template>
  <div
    v-if="props.credentials.length"
    class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
  >
    <CredentialCard
      v-for="cred in props.credentials"
      :key="cred.id"
      :credential="cred"
      @edit="emit('edit', $event)"
      @delete="emit('delete', $event)"
      @view-public-key="emit('viewPublicKey', $event)"
    />
  </div>

  <div v-else class="overflow-hidden rounded-lg border border-border bg-card">
    <EmptyState
      :icon="KeyRound"
      title="No credentials"
      description="Add credentials so they can be injected into workspaces as environment variables or SSH keys."
    />
  </div>
</template>
