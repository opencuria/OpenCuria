<!--
  WorkspacePluginCredentialSelection — plugins and credentials of a workspace
  (create and edit). A selected plugin lists the credentials it needs: those
  the plugin brings itself need nothing, the others pick one of the
  workspace's credentials right there. Enabling a plugin attaches the only
  matching credential automatically. Emits a one-line summary for hosts that
  show the selection on a parent page.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AlertCircle, Key, KeyRound, Plug, Plus, Puzzle, Search } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
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
  pluginsUsingCredential,
  toggleWorkspaceCredentialSelection,
  workspaceSelectionSummary,
  type WorkspaceSelectionSummary,
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
  /** Without a service: a new credential of any service. */
  addCredential: [serviceId?: string]
  reconnectCredential: [credentialId: string]
  validityChange: [valid: boolean]
  /** `null` while the catalog loads. */
  summaryChange: [summary: WorkspaceSelectionSummary | null]
}>()
const authStore = useAuthStore()
const credentials = useCredentialStore()
const plugins = usePluginStore()
const catalogReady = ref(false)
const catalogError = ref<string | null>(null)
const query = ref('')
const NONE = '__none__'
/** Long lists get a filter; short ones stay calm. */
const SEARCH_THRESHOLD = 8

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
const summary = computed<WorkspaceSelectionSummary | null>(() => {
  if (catalogError.value) return { text: 'Could not load', issue: 'Could not load plugins' }
  if (props.active && !catalogReady.value) return null
  return workspaceSelectionSummary(selection.value, props.pluginIds, props.credentialIds)
})

const showSearch = computed(
  () =>
    (props.pluginsEnabled ? selectablePlugins.value.length : 0) + credentials.credentials.length >
    SEARCH_THRESHOLD,
)
const needle = computed(() => (showSearch.value ? query.value.trim().toLowerCase() : ''))
function matches(...values: Array<string | undefined>): boolean {
  return !needle.value || values.some((value) => value?.toLowerCase().includes(needle.value))
}
const visiblePlugins = computed(() =>
  selectablePlugins.value.filter((plugin) => matches(plugin.name, plugin.description)),
)
const visibleCredentials = computed(() =>
  credentials.credentials.filter((credential) => matches(credential.name, credential.service_name)),
)
const enabledCount = computed(
  () => selectablePlugins.value.filter((plugin) => props.pluginIds.includes(plugin.id)).length,
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
watch(
  () => props.active,
  (active) => {
    if (!active) query.value = ''
  },
)
watch(readyToSave, (valid) => emit('validityChange', valid), { immediate: true })
watch(summary, (value) => emit('summaryChange', value), { immediate: true })
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

/** What the plugin brings: its description, otherwise its skills and tool servers. */
function pluginDetail(plugin: Plugin): string {
  if (plugin.description) return plugin.description
  const parts = [
    plugin.skills.length && `${plugin.skills.length} skill${plugin.skills.length === 1 ? '' : 's'}`,
    plugin.mcp_servers.length &&
      `${plugin.mcp_servers.length} tool server${plugin.mcp_servers.length === 1 ? '' : 's'}`,
  ]
  return parts.filter(Boolean).join(' · ')
}
function credentialIcon(credential: Credential) {
  if (credential.credential_type === 'ssh_key') return Key
  if (credential.credential_type === 'mcp_oauth') return Plug
  return KeyRound
}
/** Service, scope and the selected plugins it serves. */
function credentialDetail(credential: Credential): string {
  const users = props.credentialIds.includes(credential.id)
    ? pluginsUsingCredential(activeCatalog.value, props.pluginIds, credential)
    : []
  return [
    credential.service_name,
    credential.scope === 'organization' ? 'Organization' : 'Personal',
    users.length ? `Used by ${users.map((plugin) => plugin.name).join(', ')}` : null,
  ]
    .filter(Boolean)
    .join(' · ')
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
  <div class="space-y-6" data-testid="workspace-plugin-credential-selection">
    <div v-if="showSearch" class="relative">
      <Search
        :size="15"
        class="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-muted-foreground"
        aria-hidden="true"
      />
      <Input
        v-model="query"
        type="search"
        class="pl-9"
        placeholder="Search plugins and credentials"
        aria-label="Search plugins and credentials"
      />
    </div>

    <section v-if="pluginsEnabled" class="space-y-2.5" aria-labelledby="workspace-plugins-title">
      <div class="flex items-end justify-between gap-3">
        <div class="min-w-0">
          <h3 id="workspace-plugins-title" class="text-sm font-semibold">Plugins</h3>
          <p class="text-xs text-muted-foreground">
            Skills and tools for agents in this workspace.
          </p>
        </div>
        <span
          v-if="catalogReady && selectablePlugins.length"
          class="shrink-0 text-xs tabular-nums text-muted-foreground"
          >{{ enabledCount }} of {{ selectablePlugins.length }} on</span
        >
      </div>
      <p v-if="!catalogReady && !catalogError" class="text-xs text-muted-foreground" role="status">
        Loading plugins…
      </p>
      <div
        v-if="catalogError"
        class="space-y-2 rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2"
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
        v-if="visiblePlugins.length"
        class="divide-y divide-border overflow-hidden rounded-xl border border-border"
      >
        <div
          v-for="plugin in visiblePlugins"
          :key="plugin.id"
          :data-testid="`workspace-plugin-${plugin.id}`"
          class="transition-colors"
          :class="pluginIds.includes(plugin.id) && isAvailable(plugin) ? 'bg-muted/30' : ''"
        >
          <button
            type="button"
            class="flex w-full items-center gap-3 px-3.5 py-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:opacity-60"
            :disabled="disabled"
            :aria-pressed="pluginIds.includes(plugin.id)"
            @click="togglePlugin(plugin)"
          >
            <span
              class="flex size-8 shrink-0 items-center justify-center rounded-lg transition-colors"
              :class="
                pluginIds.includes(plugin.id)
                  ? 'bg-primary/10 text-primary'
                  : 'bg-muted text-muted-foreground'
              "
              aria-hidden="true"
              ><Puzzle :size="16"
            /></span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-sm font-medium text-foreground">{{
                plugin.name
              }}</span>
              <span v-if="!isAvailable(plugin)" class="block truncate text-xs text-destructive"
                >Unavailable · remove to save</span
              >
              <span
                v-else-if="
                  pluginIds.includes(plugin.id) &&
                  plugin.credential_requirements.some((requirement) => isMissing(requirement))
                "
                class="flex items-center gap-1 text-xs font-medium text-destructive"
                ><AlertCircle :size="12" class="shrink-0" aria-hidden="true" />Needs a
                credential</span
              >
              <span
                v-else-if="pluginDetail(plugin)"
                class="block truncate text-xs text-muted-foreground"
                >{{ pluginDetail(plugin) }}</span
              >
            </span>
            <span
              class="relative inline-flex h-[18.4px] w-8 shrink-0 items-center rounded-full border border-transparent transition-colors"
              :class="pluginIds.includes(plugin.id) ? 'bg-primary' : 'bg-input dark:bg-input/80'"
              aria-hidden="true"
              ><span
                class="block size-4 rounded-full shadow-sm transition-transform"
                :class="
                  pluginIds.includes(plugin.id)
                    ? 'translate-x-[calc(100%-2px)] bg-background dark:bg-primary-foreground'
                    : 'translate-x-0 bg-background dark:bg-foreground'
                "
            /></span>
          </button>
          <ul
            v-if="
              pluginIds.includes(plugin.id) &&
              isAvailable(plugin) &&
              plugin.credential_requirements.length
            "
            class="space-y-2 px-3.5 pb-3 sm:pl-[3.6rem]"
            :aria-label="`Credentials for ${plugin.name}`"
          >
            <li
              v-for="requirement in plugin.credential_requirements"
              :key="requirement.id"
              class="flex min-h-7 flex-wrap items-center gap-2 text-xs"
              :data-testid="`workspace-plugin-requirement-${requirement.key}`"
            >
              <KeyRound
                :size="13"
                class="shrink-0"
                :class="isMissing(requirement) ? 'text-destructive' : 'text-muted-foreground'"
                aria-hidden="true"
              />
              <span class="min-w-[8rem] flex-1 truncate text-foreground">
                {{ requirement.service_name
                }}<span v-if="!requirement.required" class="text-muted-foreground">
                  · optional</span
                >
              </span>
              <span v-if="requirement.credential_id" class="text-muted-foreground">{{
                credentialSourceLabel(requirement)
              }}</span>
              <div v-else class="ml-auto flex items-center gap-1.5">
                <Select
                  v-if="choicesFor(requirement).length"
                  :model-value="attachedFor(requirement)?.id"
                  :disabled="disabled"
                  @update:model-value="chooseFor(requirement, $event)"
                >
                  <SelectTrigger
                    size="sm"
                    class="h-7! w-44 bg-background text-xs"
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
                  :size="choicesFor(requirement).length ? 'icon-xs' : 'xs'"
                  :variant="
                    isMissing(requirement) && !choicesFor(requirement).length ? 'default' : 'ghost'
                  "
                  type="button"
                  :disabled="disabled"
                  :aria-label="`Add ${requirement.service_name} credential`"
                  :title="`Add ${requirement.service_name} credential`"
                  @click="emit('addCredential', requirement.service_id)"
                  ><Plus />{{ choicesFor(requirement).length ? '' : 'Add credential' }}</Button
                >
              </div>
            </li>
          </ul>
        </div>
      </div>
      <p
        v-else-if="catalogReady && selectablePlugins.length"
        class="rounded-xl border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground"
      >
        No plugins match “{{ query.trim() }}”.
      </p>
      <p
        v-else-if="catalogReady"
        class="rounded-xl border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground"
      >
        No plugins are available yet.
      </p>
      <p
        v-if="missingRequired.length"
        class="flex items-center gap-1.5 text-xs text-destructive"
        data-testid="workspace-plugins-blocked"
      >
        <AlertCircle :size="13" class="shrink-0" aria-hidden="true" />
        Choose or add the credentials the selected plugins need before saving.
      </p>
    </section>

    <section class="space-y-2.5" aria-labelledby="workspace-credentials-title">
      <div class="flex items-end justify-between gap-3">
        <div class="min-w-0">
          <h3 id="workspace-credentials-title" class="text-sm font-semibold">Credentials</h3>
          <p class="text-xs text-muted-foreground">
            Available to agents as environment variables, files or SSH keys. One per service.
          </p>
        </div>
        <Button
          v-if="credentials.credentials.length"
          size="xs"
          variant="ghost"
          type="button"
          class="shrink-0"
          :disabled="disabled"
          data-testid="workspace-credential-add"
          @click="emit('addCredential')"
          ><Plus />New</Button
        >
      </div>
      <div
        v-if="duplicateSelectedServices.length"
        class="rounded-xl border border-destructive/30 bg-destructive/5 p-2 text-xs text-destructive"
        role="alert"
      >
        Choose only one credential per service: {{ duplicateSelectedServices.join(', ') }}.
      </div>
      <div
        v-if="unavailableCredentialIds.length"
        class="space-y-1 rounded-xl border border-destructive/30 bg-destructive/5 p-2"
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
        v-if="visibleCredentials.length"
        class="divide-y divide-border overflow-hidden rounded-xl border border-border"
      >
        <button
          v-for="credential in visibleCredentials"
          :key="credential.id"
          type="button"
          class="flex w-full items-center gap-3 px-3.5 py-3 text-left transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:opacity-60"
          :class="credentialIds.includes(credential.id) ? 'bg-muted/30' : ''"
          :disabled="
            disabled || (!isCredentialConnected(credential) && !canManageOAuth(credential))
          "
          :aria-pressed="credentialIds.includes(credential.id)"
          @click="toggleCredential(credential)"
        >
          <span
            class="flex size-8 shrink-0 items-center justify-center rounded-lg transition-colors"
            :class="
              credentialIds.includes(credential.id)
                ? 'bg-primary/10 text-primary'
                : 'bg-muted text-muted-foreground'
            "
            aria-hidden="true"
            ><component :is="credentialIcon(credential)" :size="15"
          /></span>
          <span class="min-w-0 flex-1">
            <span class="block truncate text-sm font-medium text-foreground"
              >{{ credential.name
              }}<span v-if="credential.credential_type === 'ssh_key'" class="sr-only">
                (SSH key)</span
              ></span
            >
            <span class="block truncate text-xs text-muted-foreground">{{
              credentialDetail(credential)
            }}</span>
          </span>
          <span
            v-if="credential.credential_type === 'mcp_oauth' && !credential.oauth_connected"
            class="inline-flex shrink-0 items-center gap-1 rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive"
            >Reconnect</span
          >
          <span
            class="relative inline-flex h-[18.4px] w-8 shrink-0 items-center rounded-full border border-transparent transition-colors"
            :class="
              credentialIds.includes(credential.id) ? 'bg-primary' : 'bg-input dark:bg-input/80'
            "
            aria-hidden="true"
            ><span
              class="block size-4 rounded-full shadow-sm transition-transform"
              :class="
                credentialIds.includes(credential.id)
                  ? 'translate-x-[calc(100%-2px)] bg-background dark:bg-primary-foreground'
                  : 'translate-x-0 bg-background dark:bg-foreground'
              "
          /></span>
        </button>
      </div>
      <p
        v-else-if="credentials.credentials.length"
        class="rounded-xl border border-dashed border-border px-3 py-4 text-center text-xs text-muted-foreground"
      >
        No credentials match “{{ query.trim() }}”.
      </p>
      <div
        v-else-if="!credentials.loading"
        class="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border px-3 py-5 text-center"
      >
        <p class="text-xs text-muted-foreground">No credentials yet.</p>
        <Button
          size="xs"
          variant="outline"
          type="button"
          :disabled="disabled"
          data-testid="workspace-credential-add"
          @click="emit('addCredential')"
          ><Plus />Add credential</Button
        >
      </div>
    </section>
    <span class="sr-only" :data-ready="readyToSave" />
  </div>
</template>
