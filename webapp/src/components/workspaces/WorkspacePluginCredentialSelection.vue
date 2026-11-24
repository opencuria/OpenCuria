<!--
  WorkspacePluginCredentialSelection — plugins and credentials of a workspace
  (create and edit). A selected plugin lists the credentials it needs: those
  the plugin brings itself need nothing, the others pick one of the
  workspace's credentials right there. Enabling a plugin attaches the only
  matching credential automatically.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Check, Key, KeyRound, Plus, Puzzle } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAuthStore } from '@/stores/auth'
import { useCredentialStore } from '@/stores/credentials'
import { usePluginStore } from '@/stores/plugins'
import {
  autoAttachCredentials,
  evaluateWorkspacePluginSelection,
  isCredentialConnected,
  toggleWorkspaceCredentialSelection,
} from '@opencuria/chat-core/workspaceConfiguration'
import type { Credential } from '@/types'
import {
  credentialSourceLabel,
  type Plugin,
  type PluginCredentialRequirement,
} from '@opencuria/chat-core/plugins'

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
const NONE = '__none__'

const selection = computed(() =>
  evaluateWorkspacePluginSelection({
    plugins: plugins.plugins,
    credentials: credentials.credentials,
    organizationId: authStore.activeOrganizationId,
    pluginIds: props.pluginIds,
    credentialIds: props.credentialIds,
  }),
)
const activeCatalog = computed(() => selection.value.catalog)
const unavailableSelected = computed(() => selection.value.unavailablePluginIds)
const selectablePlugins = computed(() => [
  ...activeCatalog.value,
  ...unavailableSelected.value.map(
    (id) =>
      ({
        id,
        name: `Unavailable plugin (${id.slice(0, 8)})`,
        slug: '',
        description: '',
        scope: 'personal',
        enabled: false,
        published: false,
        organization_id: null,
        is_global: false,
        org_enabled: false,
        available: false,
        editable: false,
        skills: [],
        mcp_servers: [],
        credential_requirements: [],
        created_at: '',
        updated_at: '',
      }) as Plugin,
  ),
])
const unavailableCredentialIds = computed(() => selection.value.unavailableCredentialIds)
const duplicateSelectedServices = computed(() =>
  selection.value.duplicateServiceIds.map(
    (id) => credentials.credentials.find((entry) => entry.service_id === id)?.service_name ?? id,
  ),
)
const missingRequired = computed(() => selection.value.missingRequired)
const readyToSave = computed(
  () => (!props.active || catalogReady.value) && !catalogError.value && selection.value.valid,
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

function isAvailable(plugin: Plugin): boolean {
  return activeCatalog.value.some((entry) => entry.id === plugin.id)
}
function togglePlugin(plugin: Plugin): void {
  if (props.pluginIds.includes(plugin.id)) {
    emit(
      'update:pluginIds',
      props.pluginIds.filter((id) => id !== plugin.id),
    )
    return
  }
  emit('update:pluginIds', [...props.pluginIds, plugin.id])
  const attached = autoAttachCredentials(plugin, props.credentialIds, credentials.credentials)
  if (attached !== props.credentialIds) emit('update:credentialIds', attached)
}
function removeUnavailableCredential(id: string): void {
  emit(
    'update:credentialIds',
    props.credentialIds.filter((entry) => entry !== id),
  )
}
function toggleCredential(credential: Credential): void {
  if (!isCredentialConnected(credential) && !props.credentialIds.includes(credential.id)) {
    emit('reconnectCredential', credential.id)
    return
  }
  emit(
    'update:credentialIds',
    toggleWorkspaceCredentialSelection(props.credentialIds, credential, credentials.credentials),
  )
}
function canManageOAuth(credential: Credential): boolean {
  return credential.scope === 'personal'
    ? authStore.user?.id === credential.created_by_id
    : authStore.isAdmin
}

/** Connected credentials of the requirement's service. */
function choicesFor(requirement: PluginCredentialRequirement): Credential[] {
  return credentials.credentials.filter(
    (credential) =>
      credential.service_id === requirement.service_id && isCredentialConnected(credential),
  )
}
/** OAuth credentials of the service that must be reconnected first. */
function disconnectedFor(requirement: PluginCredentialRequirement): Credential[] {
  return credentials.credentials.filter(
    (credential) =>
      credential.service_id === requirement.service_id && !isCredentialConnected(credential),
  )
}
function attachedFor(requirement: PluginCredentialRequirement): Credential | undefined {
  return choicesFor(requirement).find((credential) => props.credentialIds.includes(credential.id))
}
function isMissing(requirement: PluginCredentialRequirement): boolean {
  return requirement.required && !requirement.credential_id && !attachedFor(requirement)
}
function chooseFor(requirement: PluginCredentialRequirement, value: unknown): void {
  const withoutService = props.credentialIds.filter(
    (id) =>
      credentials.credentials.find((credential) => credential.id === id)?.service_id !==
      requirement.service_id,
  )
  emit(
    'update:credentialIds',
    value && value !== NONE ? [...withoutService, String(value)] : withoutService,
  )
}
</script>

<template>
  <div class="space-y-5" data-testid="workspace-plugin-credential-selection">
    <section v-if="pluginsEnabled" class="space-y-2">
      <div>
        <h3 class="text-sm font-medium">
          Plugins <span class="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <p class="text-xs text-muted-foreground">Skills and tools for agents in this workspace.</p>
      </div>
      <p v-if="!catalogReady && !catalogError" class="text-xs text-muted-foreground" role="status">
        Loading plugins…
      </p>
      <div
        v-if="catalogError"
        class="space-y-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2"
        role="alert"
      >
        <p class="text-xs text-destructive">
          Could not load plugins or credentials: {{ catalogError }}
        </p>
        <Button size="sm" variant="outline" type="button" :disabled="disabled" @click="loadCatalog"
          >Retry</Button
        >
      </div>
      <div
        v-if="selectablePlugins.length"
        class="divide-y divide-border overflow-hidden rounded-lg border border-border"
      >
        <div
          v-for="plugin in selectablePlugins"
          :key="plugin.id"
          :data-testid="`workspace-plugin-${plugin.id}`"
        >
          <button
            type="button"
            class="flex w-full items-center gap-2.5 px-3 py-2.5 text-left text-sm hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            :disabled="disabled"
            :aria-pressed="pluginIds.includes(plugin.id)"
            @click="togglePlugin(plugin)"
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
            <span class="min-w-0 flex-1 truncate">{{ plugin.name }}</span>
            <span v-if="!isAvailable(plugin)" class="text-xs text-destructive"
              >Unavailable · remove to save</span
            >
            <span
              v-else-if="
                pluginIds.includes(plugin.id) &&
                plugin.credential_requirements.some((requirement) => isMissing(requirement))
              "
              class="text-xs text-destructive"
              >Needs a credential</span
            >
          </button>
          <ul
            v-if="
              pluginIds.includes(plugin.id) &&
              isAvailable(plugin) &&
              plugin.credential_requirements.length
            "
            class="space-y-1 px-3 pb-3 pl-[2.6rem]"
          >
            <li
              v-for="requirement in plugin.credential_requirements"
              :key="requirement.id"
              class="flex flex-wrap items-center gap-2 text-xs"
              :data-testid="`workspace-plugin-requirement-${requirement.key}`"
            >
              <KeyRound :size="13" class="shrink-0 text-muted-foreground" />
              <span class="min-w-0 flex-1 truncate">
                {{ requirement.service_name
                }}<span v-if="!requirement.required" class="text-muted-foreground">
                  · optional</span
                >
              </span>
              <span v-if="requirement.credential_id" class="text-muted-foreground">{{
                credentialSourceLabel(requirement)
              }}</span>
              <template v-else>
                <Select
                  v-if="choicesFor(requirement).length"
                  :model-value="attachedFor(requirement)?.id"
                  :disabled="disabled"
                  @update:model-value="chooseFor(requirement, $event)"
                >
                  <SelectTrigger
                    size="sm"
                    class="h-7! w-44 text-xs"
                    :aria-invalid="isMissing(requirement) || undefined"
                    :aria-label="`Credential for ${requirement.service_name}`"
                    ><SelectValue placeholder="Choose credential"
                  /></SelectTrigger>
                  <SelectContent>
                    <SelectItem v-if="!requirement.required" :value="NONE">None</SelectItem>
                    <SelectItem
                      v-for="credential in choicesFor(requirement)"
                      :key="credential.id"
                      :value="credential.id"
                      >{{ credential.name }}</SelectItem
                    >
                  </SelectContent>
                </Select>
                <template v-for="credential in disconnectedFor(requirement)" :key="credential.id">
                  <Button
                    v-if="canManageOAuth(credential)"
                    size="xs"
                    variant="outline"
                    type="button"
                    :disabled="disabled"
                    @click="emit('reconnectCredential', credential.id)"
                    >Reconnect {{ credential.name }}</Button
                  >
                  <span v-else class="text-muted-foreground"
                    >{{ credential.name }} needs its owner to reconnect</span
                  >
                </template>
                <Button
                  size="xs"
                  :variant="
                    isMissing(requirement) && !choicesFor(requirement).length ? 'default' : 'ghost'
                  "
                  type="button"
                  :disabled="disabled"
                  :aria-label="`Add ${requirement.service_name} credential`"
                  @click="emit('addCredential', requirement.service_id)"
                  ><Plus />{{ choicesFor(requirement).length ? '' : 'Add credential' }}</Button
                >
              </template>
            </li>
          </ul>
        </div>
      </div>
      <p v-else-if="catalogReady" class="text-xs text-muted-foreground">
        No plugins are available yet.
      </p>
      <p
        v-if="missingRequired.length"
        class="text-xs text-destructive"
        data-testid="workspace-plugins-blocked"
      >
        Choose or add the credentials the selected plugins need before saving.
      </p>
    </section>

    <section class="space-y-2">
      <div>
        <h3 class="text-sm font-medium">
          Credentials <span class="font-normal text-muted-foreground">(optional)</span>
        </h3>
        <p class="text-xs text-muted-foreground">
          Available to agents in this workspace, e.g. as environment variables or SSH keys. One per
          service.
        </p>
      </div>
      <div
        v-if="duplicateSelectedServices.length"
        class="rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive"
        role="alert"
      >
        Choose only one credential per service: {{ duplicateSelectedServices.join(', ') }}.
      </div>
      <div
        v-if="unavailableCredentialIds.length"
        class="space-y-1 rounded-md border border-destructive/30 bg-destructive/5 p-2"
        role="alert"
      >
        <p class="text-xs text-destructive">
          Some attached credentials no longer exist. Remove them before saving.
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
      <div
        v-if="credentials.credentials.length"
        class="max-h-48 divide-y divide-border overflow-y-auto rounded-lg border border-border"
      >
        <button
          v-for="credential in credentials.credentials"
          :key="credential.id"
          type="button"
          class="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm hover:bg-muted/40 disabled:opacity-60"
          :disabled="
            disabled || (!isCredentialConnected(credential) && !canManageOAuth(credential))
          "
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
          <span class="truncate text-xs text-muted-foreground">{{ credential.service_name }}</span>
          <span
            v-if="credential.credential_type === 'mcp_oauth' && !credential.oauth_connected"
            class="text-xs text-destructive"
            >Reconnect</span
          >
          <Key
            v-else-if="credential.credential_type === 'ssh_key'"
            :size="12"
            class="shrink-0 text-muted-foreground"
            aria-label="SSH key"
          />
        </button>
      </div>
      <p v-else-if="!credentials.loading" class="text-xs text-muted-foreground">
        No credentials yet.
      </p>
    </section>
    <span class="sr-only" :data-ready="readyToSave" />
  </div>
</template>
