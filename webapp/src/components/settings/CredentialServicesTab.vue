<!--
  CredentialServicesTab — catalog of credential services and per-org activation.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { get, post } from '@/services/api'
import { useAuthStore } from '@/stores/auth'
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
import { Plus, Key, X } from '@lucide/vue'

interface CredentialServiceWithActivation {
  id: string
  name: string
  slug: string
  description: string
  credential_type: string
  env_var_name: string
  target_path: string
  label: string
  is_active: boolean
}

interface CredentialServiceCreateIn {
  name: string
  slug?: string
  description?: string
  credential_type: 'env' | 'file' | 'ssh_key'
  env_var_name?: string
  target_path?: string
  label?: string
}

const authStore = useAuthStore()
const activeOrganizationId = computed(() => authStore.activeOrganizationId)

const credentialServices = ref<CredentialServiceWithActivation[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const toggleLoading = ref<string | null>(null)

const showCreateServiceModal = ref(false)
const createServiceLoading = ref(false)
const serviceName = ref('')
const serviceSlug = ref('')
const serviceDescription = ref('')
const serviceCredentialType = ref<'env' | 'file' | 'ssh_key'>('env')
const serviceEnvVarName = ref('')
const serviceTargetPath = ref('')
const serviceLabel = ref('')
const serviceSlugTouched = ref(false)

const credentialTypeOptions = [
  { value: 'env', label: 'Environment Variable' },
  { value: 'file', label: 'Credential File' },
  { value: 'ssh_key', label: 'SSH Key Pair' },
]

const generatedServiceSlug = computed(() => {
  return serviceName.value
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, '')
    .replace(/[\s_]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
})

const normalizedServiceSlug = computed(() => serviceSlug.value.trim() || generatedServiceSlug.value)

const isCreateServiceValid = computed(() => {
  if (!serviceName.value.trim()) return false
  if (!normalizedServiceSlug.value) return false
  if (serviceCredentialType.value === 'env') {
    return !!serviceEnvVarName.value.trim().match(/^[A-Z_][A-Z0-9_]*$/)
  }
  if (serviceCredentialType.value === 'file') {
    return serviceTargetPath.value.trim().length > 0
  }
  return true
})

function typeLabel(type: string): string {
  if (type === 'ssh_key') return 'SSH Key'
  if (type === 'file') return 'File'
  if (type === 'env') return 'ENV'
  return type
}

async function loadData(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    if (!activeOrganizationId.value) {
      throw new Error('No active organization selected')
    }
    credentialServices.value = await get<CredentialServiceWithActivation[]>(
      '/org-credential-services/',
    )
  } catch (e: unknown) {
    error.value = (e as Error).message || 'Failed to load settings'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadData()
})

watch(activeOrganizationId, () => {
  void loadData()
})

async function toggleCredentialServiceActivation(
  svc: CredentialServiceWithActivation,
): Promise<void> {
  toggleLoading.value = svc.id
  try {
    const updated = await post<CredentialServiceWithActivation>(
      `/org-credential-services/${svc.id}/activation/`,
      { active: !svc.is_active },
    )
    const idx = credentialServices.value.findIndex((s) => s.id === svc.id)
    if (idx !== -1) credentialServices.value[idx] = updated
  } catch {
    error.value = 'Failed to toggle credential service activation'
  } finally {
    toggleLoading.value = null
  }
}

function openCreateCredentialService(): void {
  resetCreateServiceForm()
  showCreateServiceModal.value = true
}

function resetCreateServiceForm(): void {
  serviceName.value = ''
  serviceSlug.value = ''
  serviceDescription.value = ''
  serviceCredentialType.value = 'env'
  serviceEnvVarName.value = ''
  serviceTargetPath.value = ''
  serviceLabel.value = ''
  serviceSlugTouched.value = false
}

function closeCreateCredentialService(force = false): void {
  if (createServiceLoading.value && !force) return
  showCreateServiceModal.value = false
  resetCreateServiceForm()
}

watch(serviceName, () => {
  if (!serviceSlugTouched.value) {
    serviceSlug.value = generatedServiceSlug.value
  }
})

async function createCredentialService(): Promise<void> {
  if (!isCreateServiceValid.value || createServiceLoading.value) return

  createServiceLoading.value = true
  error.value = null
  const payload: CredentialServiceCreateIn = {
    name: serviceName.value.trim(),
    slug: normalizedServiceSlug.value,
    description: serviceDescription.value.trim(),
    credential_type: serviceCredentialType.value,
    env_var_name:
      serviceCredentialType.value === 'env'
        ? serviceEnvVarName.value.trim().toUpperCase()
        : undefined,
    target_path:
      serviceCredentialType.value === 'file' ? serviceTargetPath.value.trim() : undefined,
    label: serviceLabel.value.trim(),
  }

  try {
    const created = await post<CredentialServiceWithActivation>(
      '/org-credential-services/',
      payload,
    )
    credentialServices.value = [...credentialServices.value, created].sort((a, b) =>
      a.name.localeCompare(b.name),
    )
    closeCreateCredentialService(true)
  } catch (e) {
    error.value = (e as Error).message || 'Failed to create credential service'
  } finally {
    createServiceLoading.value = false
  }
}
</script>

<template>
  <div class="space-y-6">
    <div
      v-if="error"
      class="flex items-center justify-between rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
    >
      <span>{{ error }}</span>
      <Button size="icon-sm" variant="ghost" @click="error = null">
        <X />
      </Button>
    </div>

    <div v-if="loading" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <SettingsSection
      v-else
      description="Control which credential services are available to members of this organization."
    >
      <template #actions>
        <Button size="sm" @click="openCreateCredentialService">
          <Plus />
          New Service
        </Button>
      </template>

      <div
        v-if="credentialServices.length === 0"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState
          :icon="Key"
          title="No credential services"
          description="Define a service so members can store matching credentials for workspaces."
        />
      </div>

      <div
        v-else
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <SettingsRow
          v-for="svc in credentialServices"
          :key="svc.id"
          :icon-class="svc.is_active ? 'bg-success/10 text-success' : undefined"
        >
          <template #icon>
            <Key :size="16" />
          </template>
          <div class="min-w-0 space-y-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-medium text-foreground">{{ svc.name }}</span>
              <span class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                {{ typeLabel(svc.credential_type) }}
              </span>
            </div>
            <p v-if="svc.description" class="text-sm text-muted-foreground">
              {{ svc.description }}
            </p>
            <p v-if="svc.env_var_name" class="font-mono text-xs text-muted-foreground">
              {{ svc.env_var_name }}
            </p>
            <p v-if="svc.target_path" class="font-mono text-xs text-muted-foreground break-all">
              {{ svc.target_path }}
            </p>
          </div>
          <template #actions>
            <div class="flex items-center gap-2">
              <span class="text-xs text-muted-foreground">
                {{ svc.is_active ? 'Active' : 'Inactive' }}
              </span>
              <Switch
                :model-value="svc.is_active"
                :disabled="toggleLoading === svc.id"
                :aria-label="svc.is_active ? 'Deactivate service' : 'Activate service'"
                @update:model-value="toggleCredentialServiceActivation(svc)"
              />
            </div>
          </template>
        </SettingsRow>
      </div>
    </SettingsSection>

    <Dialog
      :open="showCreateServiceModal"
      @update:open="(v) => (v ? (showCreateServiceModal = true) : closeCreateCredentialService())"
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create Credential Service</DialogTitle>
          <DialogDescription>
            Define a new credential service your organization can use in credentials and workspaces.
          </DialogDescription>
        </DialogHeader>

        <DialogBody>
        <form id="create-credential-service-form" class="space-y-4" @submit.prevent="createCredentialService">
          <div class="space-y-2">
            <Label for="service-name">Name</Label>
            <Input id="service-name" v-model="serviceName" placeholder="GitHub Enterprise" />
          </div>

          <div class="grid gap-3 sm:grid-cols-2">
            <div class="space-y-2">
              <Label for="service-slug">Slug</Label>
              <Input
                id="service-slug"
                v-model="serviceSlug"
                placeholder="github-enterprise"
                @update:model-value="serviceSlugTouched = true"
              />
              <p class="text-xs text-muted-foreground">
                Used as a stable identifier. Auto-generated from the name.
              </p>
            </div>
            <div class="space-y-2">
              <Label>Credential Type</Label>
              <Select v-model="serviceCredentialType">
                <SelectTrigger>
                  <SelectValue placeholder="Select credential type" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem
                    v-for="option in credentialTypeOptions"
                    :key="option.value"
                    :value="option.value"
                  >
                    {{ option.label }}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div v-if="serviceCredentialType === 'env'" class="space-y-2">
            <Label for="service-env">Environment Variable Name</Label>
            <Input id="service-env" v-model="serviceEnvVarName" placeholder="GITHUB_TOKEN" />
            <p class="text-xs text-muted-foreground">
              Must be uppercase snake case, e.g. <code>OPENAI_API_KEY</code>.
            </p>
          </div>

          <div v-else-if="serviceCredentialType === 'file'" class="space-y-2">
            <Label for="service-path">Target Path</Label>
            <Input id="service-path" v-model="serviceTargetPath" placeholder="~/.codex/auth.json" />
            <p class="text-xs text-muted-foreground">
              Supports absolute paths, <code>~/...</code>, <code>${HOME}/...</code>, and relative
              paths resolved against HOME.
            </p>
          </div>

          <div class="space-y-2">
            <Label for="service-label">Label</Label>
            <Input id="service-label" v-model="serviceLabel" placeholder="Personal Access Token" />
            <p class="text-xs text-muted-foreground">
              Optional helper label shown in credential forms.
            </p>
          </div>

          <div class="space-y-2">
            <Label for="service-description">Description</Label>
            <Input
              id="service-description"
              v-model="serviceDescription"
              placeholder="Used for repository access and API integrations."
            />
          </div>

        </form>
        </DialogBody>

        <DialogFooter>
          <Button
            variant="outline"
            type="button"
            :disabled="createServiceLoading"
            @click="closeCreateCredentialService"
          >
            Cancel
          </Button>
          <Button type="submit" form="create-credential-service-form" :disabled="!isCreateServiceValid || createServiceLoading">
            <LoadingSpinner v-if="createServiceLoading" :size="12" />
            <Plus v-else />
            Create Service
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
