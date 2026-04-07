<script setup lang="ts">
import type { Credential } from '@/types'
import CredentialCard from './CredentialCard.vue'
import { UiEmptyState } from '@/components/ui'
import { KeyRound } from 'lucide-vue-next'

defineProps<{
  credentials: Credential[]
}>()

const emit = defineEmits<{
  edit: [credential: Credential]
  delete: [credential: Credential]
  viewPublicKey: [credential: Credential]
}>()
</script>

<template>
  <div v-if="credentials.length" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
    <CredentialCard
      v-for="cred in credentials"
      :key="cred.id"
      :credential="cred"
      @edit="emit('edit', $event)"
      @delete="emit('delete', $event)"
      @view-public-key="emit('viewPublicKey', $event)"
    />
  </div>

  <UiEmptyState
    v-else
    title="No credentials"
    description="Add credentials so they can be injected into workspaces as environment variables or SSH keys."
  >
    <template #icon>
      <KeyRound :size="40" />
    </template>
  </UiEmptyState>
</template>
