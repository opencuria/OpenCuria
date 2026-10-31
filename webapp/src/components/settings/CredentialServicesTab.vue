<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Plus, Key, X } from '@lucide/vue'
import { useAuthStore } from '@/stores/auth'
import { useCredentialStore } from '@/stores/credentials'
import * as credentialsApi from '@/services/credentials.api'
import { slugify } from '@/lib/pluginForms'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import SettingsSection from './SettingsSection.vue'
import SettingsRow from './SettingsRow.vue'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { CredentialService, CredentialServiceCreateIn } from '@/types'

const authStore = useAuthStore()
const credentialStore = useCredentialStore()
const activeOrganizationId = computed(() => authStore.activeOrganizationId)
const services = ref<CredentialService[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const toggleLoading = ref<string | null>(null)
const showCreate = ref(false)
const creating = ref(false)
const serviceName = ref('')
const description = ref('')
const type = ref<CredentialServiceCreateIn['credential_type']>('env')
const envVarName = ref('')
const targetPath = ref('')
const oauthServerUrl = ref('')
const label = ref('')
let servicesRequestId = 0

const typeOptions = [
  { value: 'env', label: 'Environment Variable' },
  { value: 'file', label: 'Credential File' },
  { value: 'ssh_key', label: 'SSH Key Pair' },
  { value: 'mcp_oauth', label: 'MCP OAuth' },
] as const
const valid = computed(() => {
  if (!serviceName.value.trim() || !slugify(serviceName.value)) return false
  if (type.value === 'env') return /^[A-Z_][A-Z0-9_]*$/.test(envVarName.value.trim().toUpperCase())
  if (type.value === 'file') return !!targetPath.value.trim()
  if (type.value === 'mcp_oauth') {
    try {
      const url = new URL(oauthServerUrl.value)
      return (
        url.protocol === 'https:' &&
        !!url.hostname &&
        !url.username &&
        !url.password &&
        !url.search &&
        !url.hash
      )
    } catch {
      return false
    }
  }
  return true
})
function typeLabel(value: string): string {
  return typeOptions.find((item) => item.value === value)?.label ?? value
}
function ownership(service: CredentialService): string {
  return service.organization_id ? 'Organization-owned' : 'Global'
}
async function load(): Promise<void> {
  const organizationId = activeOrganizationId.value
  const requestId = ++servicesRequestId
  loading.value = true
  error.value = null
  try {
    if (!activeOrganizationId.value)
      throw new Error('Select an organization to manage credential services.')
    const [organizationServices, catalogServices] = await Promise.all([
      credentialsApi.listOrganizationCredentialServices(),
      credentialsApi.listCredentialServices(),
    ])
    if (requestId !== servicesRequestId || activeOrganizationId.value !== organizationId) return
    services.value = organizationServices
    credentialStore.services = catalogServices
    credentialStore.servicesLoaded = true
    credentialStore.servicesError = null
  } catch (e) {
    if (requestId !== servicesRequestId || activeOrganizationId.value !== organizationId) return
    credentialStore.servicesLoaded = true
    credentialStore.servicesError =
      e instanceof Error ? e.message : 'Failed to load credential services'
    error.value = credentialStore.servicesError
  } finally {
    if (requestId === servicesRequestId) loading.value = false
  }
}
onMounted(() => void load())
watch(activeOrganizationId, () => void load())
async function toggle(service: CredentialService): Promise<void> {
  toggleLoading.value = service.id
  try {
    const updated = await credentialsApi.toggleOrganizationCredentialService(
      service.id,
      !service.is_active,
    )
    const index = services.value.findIndex((item) => item.id === service.id)
    if (index !== -1) services.value[index] = updated
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to toggle service activation'
  } finally {
    toggleLoading.value = null
  }
}
function setCreateOpen(value: boolean): void {
  showCreate.value = value
  if (!value && !creating.value) reset()
}

function reset(): void {
  serviceName.value = ''
  description.value = ''
  type.value = 'env'
  envVarName.value = ''
  targetPath.value = ''
  oauthServerUrl.value = ''
  label.value = ''
}
async function create(): Promise<void> {
  if (!valid.value || creating.value) return
  creating.value = true
  error.value = null
  const data: CredentialServiceCreateIn = {
    name: serviceName.value.trim(),
    slug: '',
    description: description.value.trim(),
    credential_type: type.value,
    label: label.value.trim(),
    ...(type.value === 'env' ? { env_var_name: envVarName.value.trim().toUpperCase() } : {}),
    ...(type.value === 'file' ? { target_path: targetPath.value.trim() } : {}),
    ...(type.value === 'mcp_oauth' ? { oauth_server_url: oauthServerUrl.value.trim() } : {}),
  }
  try {
    const created = await credentialsApi.createOrganizationCredentialService(data)
    services.value = [...services.value, created].sort((a, b) => a.name.localeCompare(b.name))
    await load()
    showCreate.value = false
    reset()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to create service'
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <div class="space-y-6">
    <div
      v-if="error"
      class="flex items-center justify-between gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      role="alert"
    >
      <span>{{ error }}</span
      ><Button size="icon-sm" variant="ghost" aria-label="Dismiss error" @click="error = null"
        ><X
      /></Button>
    </div>
    <div v-if="loading" class="flex justify-center py-12"><LoadingSpinner :size="24" /></div>
    <SettingsSection
      v-else
      description="Manage organization credential services. Activation controls availability for new credentials; it is independent of plugin activation."
    >
      <template #actions
        ><Button size="sm" data-testid="service-create" @click="showCreate = true"
          ><Plus /> New Service</Button
        ></template
      >
      <div v-if="!services.length" class="overflow-hidden rounded-lg border border-border bg-card">
        <EmptyState
          :icon="Key"
          title="No credential services"
          description="Create an organization service or activate a global service."
        />
      </div>
      <div
        v-else
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <SettingsRow v-for="service in services" :key="service.id">
          <template #icon><Key :size="16" /></template>
          <div class="min-w-0 space-y-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-medium">{{ service.name }}</span
              ><span class="rounded bg-muted px-1.5 py-0.5 text-xs">{{
                typeLabel(service.credential_type)
              }}</span
              ><span class="rounded border px-1.5 py-0.5 text-xs">{{ ownership(service) }}</span
              ><span class="rounded border px-1.5 py-0.5 text-xs">{{
                service.is_active ? 'Active' : 'Inactive'
              }}</span>
            </div>
            <p v-if="service.description" class="text-sm text-muted-foreground">
              {{ service.description }}
            </p>
            <p
              v-if="service.oauth_server_url"
              class="break-all font-mono text-xs text-muted-foreground"
            >
              {{ service.oauth_server_url }}
            </p>
            <p class="text-xs text-muted-foreground">
              {{
                service.organization_id
                  ? `Owned by organization ${service.organization_id}`
                  : 'OpenCuria global service'
              }}
            </p>
          </div>
          <template #actions
            ><Switch
              :model-value="service.is_active"
              :disabled="toggleLoading === service.id"
              :aria-label="`${service.is_active ? 'Deactivate' : 'Activate'} ${service.name}`"
              :data-testid="`service-toggle-${service.id}`"
              @update:model-value="toggle(service)"
          /></template>
        </SettingsRow>
      </div>
    </SettingsSection>
    <Dialog :open="showCreate" @update:open="setCreateOpen">
      <DialogContent
        ><DialogHeader
          ><DialogTitle>Create Organization Credential Service</DialogTitle
          ><DialogDescription
            >Creates an organization-owned service (not a global catalog entry). Deactivation later
            only gates new credentials.</DialogDescription
          ></DialogHeader
        >
        <DialogBody
          ><form id="create-service-form" class="space-y-4" @submit.prevent="create">
            <div class="space-y-2">
              <Label for="service-name">Name</Label
              ><Input id="service-name" v-model="serviceName" placeholder="GitHub Enterprise" />
            </div>
            <div class="space-y-2">
              <Label>Credential Type</Label
              ><Select v-model="type"
                ><SelectTrigger><SelectValue /></SelectTrigger
                ><SelectContent
                  ><SelectItem
                    v-for="option in typeOptions"
                    :key="option.value"
                    :value="option.value"
                    >{{ option.label }}</SelectItem
                  ></SelectContent
                ></Select
              >
            </div>
            <div v-if="type === 'env'" class="space-y-2">
              <Label for="service-env">Environment variable name</Label
              ><Input id="service-env" v-model="envVarName" placeholder="GITHUB_TOKEN" />
            </div>
            <div v-else-if="type === 'file'" class="space-y-2">
              <Label for="service-path">Target path</Label
              ><Input id="service-path" v-model="targetPath" placeholder="~/.config/auth.json" />
            </div>
            <div v-else-if="type === 'mcp_oauth'" class="space-y-2">
              <Label for="service-oauth-url">OAuth MCP server endpoint</Label
              ><Input
                id="service-oauth-url"
                v-model="oauthServerUrl"
                type="url"
                placeholder="https://mcp.example.com/mcp"
              />
              <p class="text-xs text-muted-foreground">
                Fixed HTTPS endpoint without credentials, query, or fragment. OAuth requirements
                must match this exact endpoint.
              </p>
            </div>
            <div class="space-y-2">
              <Label for="service-label">Label (optional)</Label
              ><Input id="service-label" v-model="label" placeholder="API token" />
            </div>
            <div class="space-y-2">
              <Label for="service-description">Description</Label
              ><Input
                id="service-description"
                v-model="description"
                placeholder="Used for repository access and API integrations."
              />
            </div></form
        ></DialogBody>
        <DialogFooter
          ><Button variant="outline" :disabled="creating" @click="showCreate = false">Cancel</Button
          ><Button type="submit" form="create-service-form" :disabled="!valid || creating">{{
            creating ? 'Creating…' : 'Create Service'
          }}</Button></DialogFooter
        >
      </DialogContent>
    </Dialog>
  </div>
</template>
