<!--
  CredentialsPanel — credential list plus create/edit/delete/public-key dialogs.
  Polls every 10s.
-->
<script setup lang="ts">
import { computed, ref, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Button } from '@/components/ui/button'
import { useCredentialStore } from '@/stores/credentials'
import { usePluginStore } from '@/stores/plugins'
import { usePolling } from '@/composables/usePolling'
import CredentialCard from '@/components/credentials/CredentialCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { KeyRound } from '@lucide/vue'
import CreateCredentialDialog from '@/components/credentials/CreateCredentialDialog.vue'
import EditCredentialDialog from '@/components/credentials/EditCredentialDialog.vue'
import DeleteCredentialDialog from '@/components/credentials/DeleteCredentialDialog.vue'
import PublicKeyDialog from '@/components/credentials/PublicKeyDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'
import type { Credential } from '@/types'

const credentialStore = useCredentialStore()
const pluginStore = usePluginStore()
const router = useRouter()
const oauthCredentialIds = computed(() => {
  const ids = new Set<string>()
  for (const status of Object.values(pluginStore.mcpOAuthStatuses)) {
    if (status.personal.credential_id) ids.add(status.personal.credential_id)
    if (status.organization.credential_id) ids.add(status.organization.credential_id)
  }
  return ids
})
const visibleCredentials = computed(() => [
  ...credentialStore.credentials,
  ...[...oauthCredentialIds.value]
    .filter((id) => !credentialStore.credentials.some((credential) => credential.id === id))
    .map((id) => {
      for (const plugin of pluginStore.plugins) {
        for (const server of plugin.mcp_servers.filter((entry) => entry.auth_type === 'oauth')) {
          const requirement = plugin.credential_requirements.find((entry) => entry.key === server.oauth_requirement_key)
          const status = pluginStore.getMcpOAuthStatus(plugin.id, server.id)
          if (!requirement) continue
          if (status?.personal.credential_id === id) return oauthCredential(plugin.name, requirement.service_id, requirement.service_name, id, 'personal')
          if (status?.organization.credential_id === id) return oauthCredential(plugin.name, requirement.service_id, requirement.service_name, id, 'organization')
        }
      }
      return null
    })
    .filter((credential): credential is Credential => credential !== null),
])

const editingCredential = ref<Credential | null>(null)
const deletingCredential = ref<Credential | null>(null)
const viewingPublicKeyCredential = ref<Credential | null>(null)

const { start } = usePolling(() => credentialStore.fetchCredentials(), 10000)

onMounted(() => {
  start()
  void pluginStore.reload()
})

watch(
  () => pluginStore.plugins.map((plugin) => `${plugin.id}:${plugin.mcp_servers.filter((server) => server.auth_type === 'oauth').map((server) => server.id).join(',')}`).join('|'),
  () => {
    for (const plugin of pluginStore.plugins) {
      for (const server of plugin.mcp_servers.filter((entry) => entry.auth_type === 'oauth')) {
        if (!pluginStore.getMcpOAuthStatus(plugin.id, server.id)) void pluginStore.fetchMcpOAuthStatus(plugin.id, server.id)
      }
    }
  },
  { immediate: true },
)

function oauthCredential(pluginName: string, serviceId: string, serviceName: string, id: string, scope: 'personal' | 'organization'): Credential {
  return {
    id,
    name: `${pluginName} · ${serviceName}`,
    scope,
    service_id: serviceId,
    service_name: serviceName,
    service_slug: '',
    credential_type: 'mcp_oauth',
    env_var_name: '',
    target_path: '',
    has_public_key: false,
    created_by_id: 0,
    created_at: '',
    updated_at: '',
  }
}

function isOAuth(credential: Credential): boolean {
  return credential.credential_type === 'mcp_oauth'
}

function oauthConnected(credentialId: string): boolean {
  for (const status of Object.values(pluginStore.mcpOAuthStatuses)) {
    if (status.personal.credential_id === credentialId) return status.personal.connected
    if (status.organization.credential_id === credentialId) return status.organization.connected
  }
  return false
}

function managePlugins(): void {
  void router.push({ path: '/', query: { settings: 'plugins' } })
}

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
        v-if="credentialStore.loading && !visibleCredentials.length"
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

      <div v-else-if="visibleCredentials.length" class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
        <template v-for="credential in visibleCredentials" :key="credential.id">
          <div v-if="isOAuth(credential)" class="flex items-center justify-between gap-3 p-4">
            <div class="min-w-0">
              <p class="text-sm font-medium">{{ credential.name }}</p>
              <p class="text-xs text-muted-foreground">{{ credential.service_name }} · {{ credential.scope }} OAuth · {{ oauthConnected(credential.id) ? 'Connected' : 'Reconnect required' }} · Tokens are managed by the provider connection.</p>
            </div>
            <Button size="sm" variant="outline" data-testid="oauth-manage-plugins" @click="managePlugins">Manage in Plugins</Button>
          </div>
          <CredentialCard
            v-else
            :credential="credential"
            @edit="onEdit"
            @delete="onDelete"
            @view-public-key="onViewPublicKey"
          />
        </template>
      </div>
      <div v-else class="overflow-hidden rounded-lg border border-border bg-card">
        <EmptyState
          :icon="KeyRound"
          title="No credentials"
          description="Add credentials or connect an OAuth account from Settings → Plugins."
        />
      </div>
    </SettingsSection>

    <EditCredentialDialog :credential="editingCredential" @close="onEditClose" />
    <DeleteCredentialDialog :credential="deletingCredential" @close="onDeleteClose" />
    <PublicKeyDialog :credential="viewingPublicKeyCredential" @close="onPublicKeyClose" />
  </div>
</template>
