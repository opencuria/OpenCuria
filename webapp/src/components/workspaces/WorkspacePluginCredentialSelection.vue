<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Check, Key, Puzzle } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { useAuthStore } from '@/stores/auth'
import { useCredentialStore } from '@/stores/credentials'
import { usePluginStore } from '@/stores/plugins'
import { toggleWorkspaceCredentialSelection } from '@/lib/workspaceCredentialSelection'
import type { Credential, Plugin } from '@/types'

const props = withDefaults(
  defineProps<{
    pluginIds: string[]
    credentialIds: string[]
    disabled?: boolean
    pluginsEnabled?: boolean
    active?: boolean
  }>(),
  { disabled: false, pluginsEnabled: true, active: true },
)
const emit = defineEmits<{
  'update:pluginIds': [value: string[]]
  'update:credentialIds': [value: string[]]
  addCredential: [serviceId: string]
  reconnectCredential: [credentialId: string]
  validityChange: [valid: boolean]
}>()
const authStore = useAuthStore()
const credentials = useCredentialStore()
const plugins = usePluginStore()
const catalogReady = ref(false)
const catalogError = ref<string | null>(null)

const activeCatalog = computed(() =>
  plugins.plugins.filter(
    (plugin) =>
      plugin.enabled &&
      plugin.published &&
      plugin.org_enabled &&
      (plugin.organization_id === null ||
        plugin.organization_id === authStore.activeOrganizationId),
  ),
)
const unavailableSelected = computed(() =>
  props.pluginIds.filter((id) => !activeCatalog.value.some((p) => p.id === id)),
)
const selectablePlugins = computed(() => [
  ...activeCatalog.value,
  ...unavailableSelected.value.map(
    (id) =>
      ({
        id,
        name: `Unavailable plugin (${id.slice(0, 8)})`,
        slug: '',
        description: '',
        enabled: false,
        published: false,
        organization_id: null,
        is_global: false,
        org_enabled: false,
        skills: [],
        mcp_servers: [],
        credential_requirements: [],
        created_at: '',
        updated_at: '',
      }) as Plugin,
  ),
])
const selectedCredentialRecords = computed(() =>
  credentials.credentials.filter((item) => props.credentialIds.includes(item.id)),
)
const unavailableCredentialIds = computed(() =>
  props.credentialIds.filter(
    (id) => !credentials.credentials.some((credential) => credential.id === id),
  ),
)
const duplicateSelectedServices = computed(() => {
  const counts = new Map<string, number>()
  for (const credential of selectedCredentialRecords.value) {
    counts.set(credential.service_id, (counts.get(credential.service_id) ?? 0) + 1)
  }
  return [...counts.entries()].filter(([, count]) => count > 1).map(([serviceId]) => serviceId)
})
const requirementsForSelected = computed(() =>
  activeCatalog.value
    .filter((plugin) => props.pluginIds.includes(plugin.id))
    .flatMap((plugin) =>
      plugin.credential_requirements.map((requirement) => ({ plugin, requirement })),
    ),
)
const missingRequired = computed(() =>
  requirementsForSelected.value.filter(
    ({ requirement }) =>
      requirement.required &&
      !selectedCredentialRecords.value.some(
        (credential) =>
          credential.service_id === requirement.service_id &&
          (credential.credential_type !== 'mcp_oauth' || credential.oauth_connected),
      ),
  ),
)
const readyToSave = computed(
  () =>
    (!props.active || catalogReady.value) &&
    !catalogError.value &&
    missingRequired.value.length === 0 &&
    unavailableSelected.value.length === 0 &&
    unavailableCredentialIds.value.length === 0 &&
    duplicateSelectedServices.value.length === 0,
)

async function loadCatalog(): Promise<void> {
  const organizationId = authStore.activeOrganizationId
  catalogReady.value = false
  if (!props.active) return
  if (!props.pluginsEnabled) {
    await credentials.fetchCredentials()
    if (authStore.activeOrganizationId !== organizationId) return
    catalogError.value = credentials.error
    catalogReady.value = !catalogError.value
    return
  }
  if (!organizationId) {
    plugins.clear()
    catalogError.value = 'Choose an organization before configuring plugins.'
    return
  }
  catalogError.value = null
  await Promise.all([plugins.reload(), credentials.fetchCredentials()])
  if (authStore.activeOrganizationId !== organizationId) return
  catalogError.value = plugins.error || credentials.error
  catalogReady.value =
    plugins.loadedOrgId === organizationId && !plugins.loading && !catalogError.value
}
watch(
  () => authStore.activeOrganizationId,
  () => void loadCatalog(),
  { immediate: true },
)
watch(
  () => [props.pluginsEnabled, props.active] as const,
  () => void loadCatalog(),
)
watch(readyToSave, (valid) => emit('validityChange', valid), { immediate: true })
onMounted(() => {
  if (!credentials.servicesLoaded) void credentials.fetchServices()
})

function togglePlugin(pluginId: string): void {
  const next = props.pluginIds.includes(pluginId)
    ? props.pluginIds.filter((id) => id !== pluginId)
    : [...props.pluginIds, pluginId]
  emit('update:pluginIds', next)
}
function removeUnavailableCredential(id: string): void {
  emit(
    'update:credentialIds',
    props.credentialIds.filter((entry) => entry !== id),
  )
}
function toggleCredential(credential: Credential): void {
  if (!credentialConnected(credential) && !props.credentialIds.includes(credential.id)) {
    emit('reconnectCredential', credential.id)
    return
  }
  emit(
    'update:credentialIds',
    toggleWorkspaceCredentialSelection(props.credentialIds, credential, credentials.credentials),
  )
}
function credentialConnected(credential: Credential): boolean {
  return credential.credential_type !== 'mcp_oauth' || credential.oauth_connected
}
function canManageOAuth(credential: Credential): boolean {
  return credential.scope === 'personal'
    ? authStore.user?.id === credential.created_by_id
    : authStore.isAdmin
}
</script>

<template>
  <div class="space-y-4" data-testid="workspace-plugin-credential-selection">
    <section v-if="pluginsEnabled" class="space-y-2">
      <div>
        <h3 class="text-sm font-medium">
          Plugins <span class="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <p class="text-xs text-muted-foreground">
          Choose published plugins enabled for this organization.
        </p>
      </div>
      <p v-if="!catalogReady && !catalogError" class="text-xs text-muted-foreground" role="status">
        Loading plugin catalog…
      </p>
      <Button
        v-if="catalogError"
        size="sm"
        variant="outline"
        type="button"
        :disabled="disabled"
        @click="loadCatalog"
        >Retry catalog and credential loading</Button
      >
      <p
        v-if="catalogError"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        role="alert"
      >
        Could not load the plugin catalog or credentials. Retry before saving: {{ catalogError }}
      </p>
      <div v-if="selectablePlugins.length" class="space-y-2">
        <div v-for="plugin in selectablePlugins" :key="plugin.id" class="rounded-md border p-3">
          <button
            type="button"
            class="flex w-full items-center gap-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            :disabled="disabled"
            :aria-pressed="pluginIds.includes(plugin.id)"
            @click="togglePlugin(plugin.id)"
          >
            <span
              class="flex size-4 shrink-0 items-center justify-center rounded-sm border"
              :class="
                pluginIds.includes(plugin.id)
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border'
              "
              ><Check v-if="pluginIds.includes(plugin.id)" :size="10"
            /></span>
            <Puzzle :size="14" class="shrink-0 text-muted-foreground" />
            <span class="flex-1">{{ plugin.name }}</span>
            <span
              v-if="!activeCatalog.some((entry) => entry.id === plugin.id)"
              class="text-xs text-destructive"
              >Unavailable · remove to save</span
            >
            <span v-else class="text-xs text-muted-foreground"
              >{{ plugin.credential_requirements.length }} credential requirement{{
                plugin.credential_requirements.length === 1 ? '' : 's'
              }}</span
            >
          </button>
          <div
            v-if="
              pluginIds.includes(plugin.id) && activeCatalog.some((entry) => entry.id === plugin.id)
            "
            class="mt-3 space-y-2 border-t pt-2"
          >
            <p v-if="plugin.description" class="text-xs text-muted-foreground">
              {{ plugin.description }}
            </p>
            <div
              v-for="requirement in plugin.credential_requirements"
              :key="requirement.id"
              class="rounded-sm bg-muted/40 p-2"
            >
              <div class="flex flex-wrap items-center gap-2 text-xs">
                <span class="font-medium">{{ requirement.service_name }}</span>
                <span class="rounded border px-1.5 py-0.5">{{
                  requirement.required ? 'Required' : 'Optional'
                }}</span>
                <span>{{ requirement.credential_type }}</span>
              </div>
              <p v-if="requirement.description" class="mt-1 text-xs text-muted-foreground">
                {{ requirement.description }}
              </p>
              <div
                v-if="
                  requirement.required &&
                  !selectedCredentialRecords.some(
                    (c) => c.service_id === requirement.service_id && credentialConnected(c),
                  )
                "
                class="mt-2 space-y-2"
                role="status"
              >
                <p class="text-xs text-destructive">
                  Required credential missing for {{ requirement.key }}.
                </p>
                <div
                  v-if="
                    credentials.credentials.some((c) => c.service_id === requirement.service_id)
                  "
                  class="flex flex-wrap gap-2"
                >
                  <template
                    v-for="credential in credentials.credentials.filter(
                      (c) => c.service_id === requirement.service_id,
                    )"
                    :key="credential.id"
                  >
                    <Button
                      v-if="credentialConnected(credential)"
                      size="sm"
                      variant="outline"
                      type="button"
                      :disabled="disabled"
                      @click="toggleCredential(credential)"
                      >{{ credential.name }}</Button
                    >
                    <Button
                      v-else-if="canManageOAuth(credential)"
                      size="sm"
                      variant="outline"
                      type="button"
                      :disabled="disabled"
                      @click="emit('reconnectCredential', credential.id)"
                      >{{ credential.name }} · Reconnect</Button
                    >
                    <span v-else class="text-xs text-muted-foreground"
                      >{{ credential.name }} · Owner/admin must reconnect</span
                    >
                  </template>
                </div>
                <p v-else class="text-xs text-muted-foreground">
                  No matching credential is available.
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  type="button"
                  :disabled="disabled"
                  @click="emit('addCredential', requirement.service_id)"
                  >Add {{ requirement.service_name }} credential</Button
                >
              </div>
              <div v-else-if="!requirement.required" class="mt-2 space-y-2">
                <p
                  v-if="
                    selectedCredentialRecords.some(
                      (credential) =>
                        credential.service_id === requirement.service_id &&
                        credentialConnected(credential),
                    )
                  "
                  class="text-xs text-muted-foreground"
                >
                  Selected:
                  {{
                    selectedCredentialRecords.find(
                      (credential) =>
                        credential.service_id === requirement.service_id &&
                        credentialConnected(credential),
                    )?.name
                  }}
                </p>
                <template
                  v-for="credential in credentials.credentials.filter(
                    (entry) =>
                      entry.service_id === requirement.service_id && !credentialConnected(entry),
                  )"
                  :key="credential.id"
                >
                  <Button
                    v-if="canManageOAuth(credential)"
                    size="sm"
                    variant="outline"
                    type="button"
                    :disabled="disabled"
                    @click="emit('reconnectCredential', credential.id)"
                    >{{ credential.name }} · Reconnect</Button
                  >
                  <span v-else class="text-xs text-muted-foreground">
                    {{ credential.name }} · Owner/admin must reconnect
                  </span>
                </template>
                <Button
                  size="sm"
                  variant="outline"
                  type="button"
                  :disabled="disabled"
                  @click="emit('addCredential', requirement.service_id)"
                  >Add optional credential</Button
                >
              </div>
              <p v-else class="mt-2 text-xs text-muted-foreground">
                Selected:
                {{
                  selectedCredentialRecords.find(
                    (credential) =>
                      credential.service_id === requirement.service_id &&
                      credentialConnected(credential),
                  )?.name
                }}
              </p>
            </div>
          </div>
        </div>
      </div>
      <p v-else-if="catalogReady" class="text-xs text-muted-foreground">
        No enabled and published plugins are available in this organization.
      </p>
      <p
        v-if="missingRequired.length"
        class="text-xs text-destructive"
        data-testid="workspace-plugins-blocked"
      >
        Add or select all required credentials before saving.
      </p>
    </section>

    <section class="space-y-2">
      <div>
        <h3 class="text-sm font-medium">
          Workspace credentials <span class="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <p class="text-xs text-muted-foreground">
          Credentials are private server-side references; one credential per service is attached.
        </p>
      </div>
      <div
        v-if="duplicateSelectedServices.length"
        class="space-y-1 rounded-md border border-destructive/30 bg-destructive/5 p-2"
        role="alert"
      >
        <p class="text-xs text-destructive">Choose only one attached credential per service.</p>
        <p v-for="serviceId in duplicateSelectedServices" :key="serviceId" class="text-xs">
          Service {{ serviceId.slice(0, 8) }} has duplicate selections.
        </p>
      </div>
      <div
        v-if="unavailableCredentialIds.length"
        class="space-y-1 rounded-md border border-destructive/30 bg-destructive/5 p-2"
        role="alert"
      >
        <p class="text-xs text-destructive">
          Some attached credentials are unavailable. Remove them before saving.
        </p>
        <div
          v-for="id in unavailableCredentialIds"
          :key="id"
          class="flex items-center justify-between gap-2 text-xs"
        >
          <span>Unavailable credential ({{ id.slice(0, 8) }})</span>
          <Button
            size="sm"
            variant="ghost"
            type="button"
            :disabled="disabled"
            @click="removeUnavailableCredential(id)"
            >Remove</Button
          >
        </div>
      </div>
      <p
        v-if="credentials.loading && !credentials.credentials.length"
        class="text-xs text-muted-foreground"
        role="status"
      >
        Loading credentials…
      </p>
      <div v-if="credentials.credentials.length" class="max-h-48 space-y-1.5 overflow-y-auto">
        <button
          v-for="credential in credentials.credentials"
          :key="credential.id"
          type="button"
          class="flex w-full items-center gap-2 rounded-sm border px-3 py-2 text-left text-sm"
          :class="
            credentialIds.includes(credential.id)
              ? 'border-primary bg-primary/5'
              : 'border-border hover:bg-muted'
          "
          :disabled="disabled || (!credentialConnected(credential) && !canManageOAuth(credential))"
          :aria-pressed="credentialIds.includes(credential.id)"
          @click="toggleCredential(credential)"
        >
          <span
            class="flex size-4 shrink-0 items-center justify-center rounded-sm border"
            :class="
              credentialIds.includes(credential.id)
                ? 'border-primary bg-primary text-primary-foreground'
                : 'border-border'
            "
            ><Check v-if="credentialIds.includes(credential.id)" :size="10"
          /></span>
          <span class="min-w-0 flex-1 truncate">{{ credential.name }}</span>
          <span class="text-xs text-muted-foreground"
            >{{ credential.service_name }} · {{ credential.scope }}</span
          >
          <span
            v-if="credential.credential_type === 'mcp_oauth'"
            class="text-xs"
            :class="credential.oauth_connected ? 'text-muted-foreground' : 'text-destructive'"
            >{{ credential.oauth_connected ? 'OAuth connected' : 'Reconnect required' }}</span
          >
          <span
            v-else-if="credential.credential_type === 'ssh_key'"
            class="inline-flex items-center gap-1 text-xs text-muted-foreground"
            ><Key :size="10" /> SSH key</span
          >
        </button>
      </div>
      <p v-else-if="!credentials.loading" class="text-xs text-muted-foreground">
        No credentials available.
      </p>
    </section>
    <span class="sr-only" :data-ready="readyToSave" />
  </div>
</template>
