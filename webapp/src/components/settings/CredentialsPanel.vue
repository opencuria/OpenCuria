<!--
  CredentialsPanel — credential list plus create/edit/delete/public-key dialogs.
  Polls every 10s.
-->
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useCredentialStore } from '@/stores/credentials'
import { usePolling } from '@/composables/usePolling'
import CredentialList from '@/components/credentials/CredentialList.vue'
import CreateCredentialDialog from '@/components/credentials/CreateCredentialDialog.vue'
import EditCredentialDialog from '@/components/credentials/EditCredentialDialog.vue'
import DeleteCredentialDialog from '@/components/credentials/DeleteCredentialDialog.vue'
import PublicKeyDialog from '@/components/credentials/PublicKeyDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'
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
    <SettingsSection
      description="Manage credentials injected into workspaces. Personal credentials are yours across all organizations; organization credentials are shared with all members."
    >
      <template #actions>
        <CreateCredentialDialog />
      </template>

      <div
        v-if="credentialStore.loading && !credentialStore.credentials.length"
        class="flex justify-center py-12"
      >
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="credentialStore.error"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
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
    </SettingsSection>

    <EditCredentialDialog :credential="editingCredential" @close="onEditClose" />
    <DeleteCredentialDialog :credential="deletingCredential" @close="onDeleteClose" />
    <PublicKeyDialog :credential="viewingPublicKeyCredential" @close="onPublicKeyClose" />
  </div>
</template>
