<script setup lang="ts">
import type { Credential } from '@/types'
import CredentialCard from './CredentialCard.vue'
import { KeyRound } from '@lucide/vue'

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

  <div
    v-else
    class="flex flex-col items-center justify-center py-12 px-6 text-center"
  >
    <div class="mb-4 text-muted-foreground">
      <KeyRound :size="40" />
    </div>
    <h3 class="text-lg font-medium text-foreground mb-1">No credentials</h3>
    <p class="text-sm text-muted-foreground max-w-sm">
      Add credentials so they can be injected into workspaces as environment variables or SSH keys.
    </p>
  </div>
</template>
