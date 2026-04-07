<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useCredentialStore } from '@/stores/credentials'
import { usePolling } from '@/composables/usePolling'
import CredentialList from '@/components/credentials/CredentialList.vue'
import CreateCredentialDialog from '@/components/credentials/CreateCredentialDialog.vue'
import EditCredentialDialog from '@/components/credentials/EditCredentialDialog.vue'
import DeleteCredentialDialog from '@/components/credentials/DeleteCredentialDialog.vue'
import PublicKeyDialog from '@/components/credentials/PublicKeyDialog.vue'
import { UiSpinner } from '@/components/ui'
import type { Credential } from '@/types'

const credentialStore = useCredentialStore()

const editingCredential = ref<Credential | null>(null)
const deletingCredential = ref<Credential | null>(null)
const viewingPublicKeyCredential = ref<Credential | null>(null)

const { start } = usePolling(() => credentialStore.fetchCredentials(), 10000)

onMounted(() => {
  start()
})

function onEdit(credential: Credential): void {
  editingCredential.value = credential
}

function onDelete(credential: Credential): void {
  deletingCredential.value = credential
}

function onEditClose(): void {
  editingCredential.value = null
}

function onDeleteClose(): void {
  deletingCredential.value = null
}

function onViewPublicKey(credential: Credential): void {
  viewingPublicKeyCredential.value = credential
}

function onPublicKeyClose(): void {
  viewingPublicKeyCredential.value = null
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-fg">Credentials</h2>
        <p class="text-sm text-muted-fg mt-1">
          Manage credentials injected into workspaces. Personal credentials are yours across all
          organizations; organization credentials are shared with all members.
        </p>
      </div>
      <CreateCredentialDialog />
    </div>

    <div v-if="credentialStore.loading && !credentialStore.credentials.length" class="flex justify-center py-12">
      <UiSpinner :size="24" />
    </div>

    <div
      v-else-if="credentialStore.error"
      class="rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ credentialStore.error }}
    </div>

    <CredentialList
      v-else
      :credentials="credentialStore.credentials"
      @edit="onEdit"
      @delete="onDelete"
      @view-public-key="onViewPublicKey"
    />

    <!-- Edit dialog -->
    <EditCredentialDialog
      :credential="editingCredential"
      @close="onEditClose"
    />

    <!-- Delete confirmation dialog -->
    <DeleteCredentialDialog
      :credential="deletingCredential"
      @close="onDeleteClose"
    />

    <!-- Public key dialog -->
    <PublicKeyDialog
      :credential="viewingPublicKeyCredential"
      @close="onPublicKeyClose"
    />
  </div>
</template>
