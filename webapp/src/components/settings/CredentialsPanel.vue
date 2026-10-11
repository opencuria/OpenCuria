<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { KeyRound } from '@lucide/vue'
import { useCredentialStore } from '@/stores/credentials'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'
import { usePolling } from '@/composables/usePolling'
import CredentialCard from '@/components/credentials/CredentialCard.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import CreateCredentialDialog from '@/components/credentials/CreateCredentialDialog.vue'
import EditCredentialDialog from '@/components/credentials/EditCredentialDialog.vue'
import DeleteCredentialDialog from '@/components/credentials/DeleteCredentialDialog.vue'
import PublicKeyDialog from '@/components/credentials/PublicKeyDialog.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { Credential } from '@/types'
import { readWorkspaceDraft, removeWorkspaceDraft } from '@/lib/workspaceDraft'
import { CLOSE_SETTINGS_EVENT } from './settingsTabs'

const props = defineProps<{
  initialServiceId?: string
  initialCredentialId?: string
  workspaceDraftId?: string
  contextVersion?: number
}>()
const emit = defineEmits<{ contextConsumed: [kind: 'service' | 'credential' | 'finished'] }>()
const router = useRouter()
const route = useRoute()

const credentialStore = useCredentialStore()
const authStore = useAuthStore()
const notifications = useNotificationStore()
const createOpen = ref(false)
const selectedServiceId = ref<string | undefined>()
const reconnectingCredential = ref<string | undefined>()
const disconnectingCredential = ref<Credential | null>(null)
const disconnecting = ref(false)
const editingCredential = ref<Credential | null>(null)
const deletingCredential = ref<Credential | null>(null)
const viewingPublicKeyCredential = ref<Credential | null>(null)
const oauthError = ref<string | null>(null)
const loadedCredentials = ref(false)
const pendingReconnectId = ref<string | undefined>()

const visibleCredentials = computed(() => credentialStore.credentials)
const draftResult = computed(() => {
  if (
    !props.workspaceDraftId ||
    !authStore.initialized ||
    !authStore.user ||
    !authStore.activeOrganizationId
  )
    return null
  return readWorkspaceDraft(props.workspaceDraftId, {
    userId: authStore.user?.id,
    organizationId: authStore.activeOrganizationId,
  })
})
const activeDraft = computed(() =>
  draftResult.value?.status === 'ok' ? draftResult.value.draft : null,
)
const draftProblem = computed(() => {
  if (!draftResult.value || draftResult.value.status === 'ok') return null
  return `Workspace draft is unavailable (${draftResult.value.status}). No workspace was changed.`
})
let credentialLoad: Promise<void> | null = null
let servicesLoad: Promise<void> | null = null

async function loadCredentials(): Promise<void> {
  if (credentialLoad) return credentialLoad
  credentialLoad = credentialStore.fetchCredentials().finally(() => {
    credentialLoad = null
  })
  await credentialLoad
  if (authStore.activeOrganizationId && !loadedCredentials.value) {
    loadedCredentials.value = true
    if (pendingReconnectId.value) {
      void reconnect(pendingReconnectId.value)
      emit('contextConsumed', 'credential')
    }
  }
}

async function loadServices(): Promise<void> {
  if (credentialStore.servicesLoaded) return
  if (servicesLoad) return servicesLoad
  servicesLoad = credentialStore.fetchServices().finally(() => {
    servicesLoad = null
  })
  await servicesLoad
}

const { start } = usePolling(loadCredentials, 10000)

onMounted(() => {
  start()
})

watch(
  () => authStore.activeOrganizationId,
  (organizationId) => {
    if (!organizationId) return
    loadedCredentials.value = false
    credentialStore.credentials = []
    credentialStore.services = []
    credentialStore.servicesLoaded = false
    void Promise.all([loadCredentials(), loadServices()])
  },
  { immediate: true },
)

watch(
  () => [props.initialServiceId, props.contextVersion] as const,
  ([serviceId]) => {
    if (!serviceId) return
    selectedServiceId.value = serviceId
    createOpen.value = true
    emit('contextConsumed', 'service')
  },
  { immediate: true },
)

watch(
  () => [props.initialCredentialId, props.contextVersion] as const,
  ([credentialId]) => {
    if (credentialId) {
      pendingReconnectId.value = credentialId
      if (loadedCredentials.value) {
        void reconnect(credentialId)
        emit('contextConsumed', 'credential')
      }
    }
  },
  { immediate: true },
)

watch(
  () => route.query.oauth_result,
  async (result) => {
    if (result !== 'connected' && result !== 'error') return
    if (result === 'connected') {
      notifications.success('OAuth connected', 'Your account connection is ready to use.')
    } else {
      notifications.error(
        'OAuth connection failed',
        'The provider connection was not completed. You can try again.',
      )
    }
    await Promise.all([credentialStore.fetchCredentials(), credentialStore.fetchServices()])
    const query = { ...route.query }
    delete query.oauth_result
    delete query.credential_id
    void router.replace({ path: route.path, query }).catch(() => undefined)
  },
  { immediate: true },
)

watch(
  () => credentialStore.error,
  (error) => {
    if (error) oauthError.value = error
  },
)

function canManage(credential: Credential): boolean {
  return credential.scope === 'personal'
    ? authStore.user?.id === credential.created_by_id
    : authStore.isAdmin
}
function onEdit(credential: Credential): void {
  editingCredential.value = credential
}
function onDelete(credential: Credential): void {
  deletingCredential.value = credential
}
function onViewPublicKey(credential: Credential): void {
  viewingPublicKeyCredential.value = credential
}

async function reconnect(credentialId: string): Promise<void> {
  if (reconnectingCredential.value === credentialId) return
  if (pendingReconnectId.value === credentialId) pendingReconnectId.value = undefined
  const credential = credentialStore.credentials.find((item) => item.id === credentialId)
  if (!credential || credential.credential_type !== 'mcp_oauth' || !canManage(credential)) {
    oauthError.value =
      'This OAuth credential is unavailable or you do not have permission to reconnect it.'
    return
  }
  reconnectingCredential.value = credentialId
  const ok = await credentialStore.reconnectOAuthCredential(credentialId, credential.name)
  if (!ok) {
    oauthError.value = 'Reconnection could not be started. Check your access and try again.'
  }
  reconnectingCredential.value = undefined
}

async function confirmDisconnect(): Promise<void> {
  if (!disconnectingCredential.value) return
  disconnecting.value = true
  const ok = await credentialStore.disconnectOAuthCredential(disconnectingCredential.value.id)
  disconnecting.value = false
  if (ok) disconnectingCredential.value = null
  else
    oauthError.value =
      'This OAuth credential could not be disconnected. Check ownership and workspace requirements.'
}

function clearAddService(): void {
  selectedServiceId.value = undefined
}
function cancelWorkspaceDraft(): void {
  if (props.workspaceDraftId) removeWorkspaceDraft(props.workspaceDraftId)
  emit('contextConsumed', 'finished')
}
function dismissUnavailableDraft(): void {
  if (props.workspaceDraftId) removeWorkspaceDraft(props.workspaceDraftId)
  emit('contextConsumed', 'finished')
}

async function backToWorkspace(): Promise<void> {
  const draft = activeDraft.value
  const draftId = props.workspaceDraftId
  if (!draft || !draftId) {
    oauthError.value = 'The workspace draft is unavailable. No workspace was created or modified.'
    return
  }
  window.dispatchEvent(new Event(CLOSE_SETTINGS_EVENT))
  emit('contextConsumed', 'finished')
  await nextTick()
  await router
    .push({ path: draft.returnPath, query: { resume_workspace: draftId } })
    .catch(() => undefined)
}
</script>

<template>
  <div class="space-y-6">
    <SettingsSection description="Manage personal and organization credentials.">
      <template #actions>
        <div v-if="activeDraft" class="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            data-testid="workspace-draft-back"
            @click="backToWorkspace"
            >Back to workspace configuration</Button
          >
          <Button
            size="sm"
            variant="ghost"
            data-testid="workspace-draft-cancel"
            @click="cancelWorkspaceDraft"
            >Cancel workspace configuration</Button
          >
        </div>
        <CreateCredentialDialog
          v-model:open="createOpen"
          :preselected-service-id="selectedServiceId"
          :show-trigger="false"
          @close="clearAddService"
        />
        <Button
          size="sm"
          variant="outline"
          data-testid="credentials-add-trigger"
          @click="createOpen = true"
          >Add Credential</Button
        >
      </template>

      <div
        v-if="draftProblem"
        class="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        role="alert"
        data-testid="workspace-draft-error"
      >
        <span>{{ draftProblem }}</span>
        <Button size="sm" variant="outline" @click="dismissUnavailableDraft">Dismiss draft</Button>
      </div>
      <div
        v-if="oauthError"
        class="mb-4 flex items-center justify-between gap-3 rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        role="alert"
        data-testid="oauth-error"
      >
        <span>{{ oauthError }}</span
        ><Button size="sm" variant="ghost" @click="oauthError = null">Dismiss</Button>
      </div>
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
      <div
        v-else-if="visibleCredentials.length"
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <CredentialCard
          v-for="credential in visibleCredentials"
          :key="credential.id"
          :credential="credential"
          :reconnecting="reconnectingCredential === credential.id"
          @edit="onEdit"
          @delete="onDelete"
          @view-public-key="onViewPublicKey"
          @reconnect="reconnect($event.id)"
          @disconnect="disconnectingCredential = $event"
        />
      </div>
      <div v-else class="overflow-hidden rounded-lg border border-border bg-card">
        <EmptyState
          :icon="KeyRound"
          title="No credentials"
          description="Add a credential for an active service."
        />
      </div>
    </SettingsSection>

    <EditCredentialDialog :credential="editingCredential" @close="editingCredential = null" />
    <DeleteCredentialDialog :credential="deletingCredential" @close="deletingCredential = null" />
    <PublicKeyDialog
      :credential="viewingPublicKeyCredential"
      @close="viewingPublicKeyCredential = null"
    />
    <Dialog
      :open="!!disconnectingCredential"
      @update:open="(value) => !value && (disconnectingCredential = null)"
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Disconnect Credential</DialogTitle>
          <DialogDescription>Workspace associations are preserved.</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div
            class="p-4 rounded-[var(--radius-md)] bg-destructive/10 border border-destructive/30"
          >
            <p class="text-sm text-foreground">
              Are you sure you want to disconnect
              <strong>{{ disconnectingCredential?.name }}</strong
              >?
            </p>
            <p class="text-xs text-muted-foreground mt-1">
              This removes the provider connection. Reconnect to use it again.
            </p>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button
            variant="outline"
            :disabled="disconnecting"
            @click="disconnectingCredential = null"
            >Cancel</Button
          >
          <Button
            variant="destructive"
            :disabled="disconnecting"
            data-testid="oauth-disconnect-confirm"
            @click="confirmDisconnect"
            >{{ disconnecting ? 'Disconnecting…' : 'Disconnect' }}</Button
          >
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
