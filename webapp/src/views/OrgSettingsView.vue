<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { get, post, del } from '@/services/api'
import { getOrganization, updateOrganizationWorkspacePolicy } from '@/services/organizations.api'
import { useAuthStore } from '@/stores/auth'
import { Button } from '@/components/ui/button'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import ImageDefinitionsTab from '@/components/images/ImageDefinitionsTab.vue'
import type { Organization } from '@/types'
import { formatMinutesAsDuration } from '@/lib/utils'
import {
  Plus,
  ChevronDown,
  ChevronUp,
  Key,
  Check,
  X,
  HardDrive,
  Clock3,
} from '@lucide/vue'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const authStore = useAuthStore()
const isAdmin = computed(() => authStore.isAdmin)
const activeOrganizationId = computed(() => authStore.activeOrganizationId)

const credentialServices = ref<CredentialServiceWithActivation[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const organizationSettings = ref<Organization | null>(null)
const activeTab = ref<'workspace-policies' | 'image-definitions' | 'credential-services'>(
  'workspace-policies',
)


const toggleLoading = ref<string | null>(null)
const policySaving = ref(false)
const autoStopEnabled = ref(false)
const autoStopTimeoutMinutes = ref<number>(240)

const workspacePolicyPresetOptions = [
  { value: 30, label: '30 min' },
  { value: 60, label: '1h' },
  { value: 240, label: '4h' },
  { value: 480, label: '8h' },
  { value: 1440, label: '24h' },
]

const workspacePolicySummary = computed(() =>
  autoStopEnabled.value
    ? `Inactive workspaces stop after ${formatMinutesAsDuration(autoStopTimeoutMinutes.value)}.`
    : 'Inactive workspaces keep running until someone stops them.',
)

// Create credential service dialog
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

// ---------------------------------------------------------------------------
// Load data
// ---------------------------------------------------------------------------

async function loadData() {
  loading.value = true
  error.value = null
  try {
    if (!activeOrganizationId.value) {
      throw new Error('No active organization selected')
    }
    const [services] = await Promise.all([
      get<CredentialServiceWithActivation[]>('/org-credential-services/'),
    ])
    credentialServices.value = services
    organizationSettings.value = await getOrganization(activeOrganizationId.value)
    autoStopEnabled.value = organizationSettings.value.workspace_auto_stop_timeout_minutes != null
    autoStopTimeoutMinutes.value = organizationSettings.value.workspace_auto_stop_timeout_minutes ?? 240
  } catch (e: unknown) {
    error.value = (e as Error).message || 'Failed to load settings'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadData()
})

watch(activeOrganizationId, () => {
  void loadData()
})

async function saveWorkspacePolicy() {
  if (!activeOrganizationId.value || policySaving.value) return
  if (autoStopEnabled.value && (!Number.isFinite(autoStopTimeoutMinutes.value) || autoStopTimeoutMinutes.value < 1)) {
    error.value = 'Auto-stop timeout must be at least 1 minute.'
    return
  }

  policySaving.value = true
  error.value = null
  try {
    const updated = await updateOrganizationWorkspacePolicy(activeOrganizationId.value, {
      workspace_auto_stop_timeout_minutes: autoStopEnabled.value ? Math.round(autoStopTimeoutMinutes.value) : null,
    })
    organizationSettings.value = updated
    autoStopEnabled.value = updated.workspace_auto_stop_timeout_minutes != null
    autoStopTimeoutMinutes.value = updated.workspace_auto_stop_timeout_minutes ?? autoStopTimeoutMinutes.value
  } catch (e) {
    error.value = (e as Error).message || 'Failed to update workspace policy'
  } finally {
    policySaving.value = false
  }
}

// ---------------------------------------------------------------------------
// Credential service activation toggle
// ---------------------------------------------------------------------------

async function toggleCredentialServiceActivation(svc: CredentialServiceWithActivation) {
  toggleLoading.value = svc.id
  try {
    const updated = await post<CredentialServiceWithActivation>(
      `/org-credential-services/${svc.id}/activation/`,
      { active: !svc.is_active }
    )
    const idx = credentialServices.value.findIndex((s) => s.id === svc.id)
    if (idx !== -1) credentialServices.value[idx] = updated
  } catch (e) {
    error.value = 'Failed to toggle credential service activation'
  } finally {
    toggleLoading.value = null
  }
}

function openCreateCredentialService() {
  resetCreateServiceForm()
  showCreateServiceModal.value = true
}

function resetCreateServiceForm() {
  serviceName.value = ''
  serviceSlug.value = ''
  serviceDescription.value = ''
  serviceCredentialType.value = 'env'
  serviceEnvVarName.value = ''
  serviceTargetPath.value = ''
  serviceLabel.value = ''
  serviceSlugTouched.value = false
}

function closeCreateCredentialService(force = false) {
  if (createServiceLoading.value && !force) return
  showCreateServiceModal.value = false
  resetCreateServiceForm()
}

watch(serviceName, () => {
  if (!serviceSlugTouched.value) {
    serviceSlug.value = generatedServiceSlug.value
  }
})

async function createCredentialService() {
  if (!isCreateServiceValid.value || createServiceLoading.value) return

  createServiceLoading.value = true
  error.value = null
  const payload: CredentialServiceCreateIn = {
    name: serviceName.value.trim(),
    slug: normalizedServiceSlug.value,
    description: serviceDescription.value.trim(),
    credential_type: serviceCredentialType.value,
    env_var_name:
      serviceCredentialType.value === 'env' ? serviceEnvVarName.value.trim().toUpperCase() : undefined,
    target_path:
      serviceCredentialType.value === 'file' ? serviceTargetPath.value.trim() : undefined,
    label: serviceLabel.value.trim(),
  }

  try {
    const created = await post<CredentialServiceWithActivation>('/org-credential-services/', payload)
    credentialServices.value = [...credentialServices.value, created].sort((a, b) =>
      a.name.localeCompare(b.name)
    )
    closeCreateCredentialService(true)
  } catch (e) {
    error.value = (e as Error).message || 'Failed to create credential service'
  } finally {
    createServiceLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getCredentialServiceName(id: string): string {
  const svc = credentialServices.value.find((s) => s.id === id)
  return svc ? svc.name : id
}

</script>

<template>
  <div class="space-y-6">
    <!-- Page header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-foreground">Organization Settings</h2>
        <p class="text-sm text-muted-foreground mt-1">
          Manage workspace policy, harness provider config, and credential services for your organization.
        </p>
      </div>
    </div>

    <!-- Not admin warning -->
    <div
      v-if="!isAdmin"
      class="rounded-md border border-warning/30 bg-warning-muted px-4 py-3 text-sm text-warning"
    >
      Only organization admins can manage these settings.
    </div>

    <!-- Loading -->
    <div v-else-if="loading" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <!-- Error -->
    <div
      v-else-if="error"
      class="rounded-md border border-error/30 bg-error-muted px-4 py-3 text-sm text-error flex items-center justify-between"
    >
      <span>{{ error }}</span>
      <Button size="icon-sm" variant="ghost" @click="error = null">
        <X :size="14" />
      </Button>
    </div>

    <!-- Main content -->
    <template v-else>
      <!-- Tabs -->
      <div class="flex gap-1 border-b border-border">
        <button
          v-for="tab in [
            { key: 'workspace-policies', label: 'Workspace Policies', icon: Clock3 },
            { key: 'image-definitions', label: 'Image Definitions', icon: HardDrive },
            { key: 'credential-services', label: 'Credential Services', icon: Key },
          ]"
          :key="tab.key"
          type="button"
          class="flex items-center gap-1.5 px-4 py-2 text-sm font-medium border-b-2 transition-colors"
          :class="
            activeTab === tab.key
              ? 'border-primary text-primary'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          "
          @click="activeTab = tab.key as 'workspace-policies' | 'image-definitions' | 'credential-services'"
        >
          <component :is="tab.icon" :size="14" />
          {{ tab.label }}
        </button>
      </div>

      <div v-if="activeTab === 'workspace-policies'" class="space-y-4">
        <div class="rounded-lg border border-border bg-card p-5">
          <div class="flex items-start justify-between gap-4">
            <div>
              <h3 class="text-base font-semibold text-foreground">Automatic Workspace Stop</h3>
              <p class="mt-1 text-sm text-muted-foreground">
                Running workspaces stop automatically after the configured period without prompts,
                terminal input, or file interactions.
              </p>
            </div>
            <Badge :variant="autoStopEnabled ? 'secondary' : 'secondary'">
              {{ autoStopEnabled ? 'Enabled' : 'Disabled' }}
            </Badge>
          </div>

          <div class="mt-5 space-y-4">
            <label class="flex items-center justify-between gap-4 rounded-md border border-border px-4 py-3">
              <div>
                <div class="text-sm font-medium text-foreground">Automatically stop inactive workspaces</div>
                <div class="text-xs text-muted-foreground mt-0.5">
                  This policy applies to every workspace in the active organization.
                </div>
              </div>
              <input
                v-model="autoStopEnabled"
                type="checkbox"
                class="h-4 w-4 accent-primary"
              />
            </label>

            <div v-if="autoStopEnabled" class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_220px]">
              <div class="space-y-3">
                <label class="block">
                  <span class="text-sm font-medium text-foreground">Inactivity Timeout (minutes)</span>
                  <Input
                    :model-value="String(autoStopTimeoutMinutes)"
                    type="number"
                    min="1"
                    step="1"
                    class="mt-1.5"
                    @update:modelValue="autoStopTimeoutMinutes = Math.max(1, Number($event) || 1)"
                  />
                </label>
                <div class="flex flex-wrap gap-2">
                  <Button
                    v-for="preset in workspacePolicyPresetOptions"
                    :key="preset.value"
                    size="sm"
                    variant="outline"
                    @click="autoStopTimeoutMinutes = preset.value"
                  >
                    {{ preset.label }}
                  </Button>
                </div>
              </div>

              <div class="rounded-md border border-border bg-muted/10 px-4 py-3">
                <div class="text-xs uppercase tracking-[0.16em] text-muted-foreground">Policy Summary</div>
                <div class="mt-2 text-sm text-foreground">{{ workspacePolicySummary }}</div>
                <div class="mt-2 text-xs text-muted-foreground">
                  Active prompt sessions prevent auto-stop until they finish.
                </div>
              </div>
            </div>

            <div v-else class="rounded-md border border-border bg-muted/10 px-4 py-3 text-sm text-muted-foreground">
              Auto-stop is disabled. Workspaces remain running until users stop them manually.
            </div>

            <div class="flex items-center justify-between gap-3">
              <p class="text-xs text-muted-foreground">
                Last saved:
                {{
                  organizationSettings?.workspace_auto_stop_timeout_minutes != null
                    ? formatMinutesAsDuration(organizationSettings.workspace_auto_stop_timeout_minutes)
                    : 'Disabled'
                }}
              </p>
              <Button size="sm" :disabled="policySaving" @click="saveWorkspacePolicy">
                <LoadingSpinner v-if="policySaving" :size="12" />
                <span v-else>Save Policy</span>
              </Button>
            </div>
          </div>
        </div>
      </div>

      <!-- ================================================================ -->
      <!-- Image Definitions Tab -->
      <!-- (credential services below) -->
      <!-- ================================================================ -->
      <div v-else-if="activeTab === 'image-definitions'" class="space-y-4">
        <ImageDefinitionsTab />
      </div>

      <div v-else-if="activeTab === 'credential-services'" class="space-y-4">
        <div class="flex items-start justify-between gap-3">
          <p class="text-sm text-muted-foreground">
            Control which credential services are available to your organization members.
          </p>
          <Button size="sm" @click="openCreateCredentialService">
            <Plus :size="14" />
            New Service
          </Button>
        </div>

        <div class="space-y-2">
          <div
            v-for="svc in credentialServices"
            :key="svc.id"
            class="flex items-center gap-3 px-4 py-3 rounded-md border border-border bg-card"
          >
            <!-- Icon -->
            <div
              class="w-8 h-8 rounded-full flex items-center justify-center shrink-0"
              :class="svc.is_active ? 'bg-success/10' : 'bg-muted/10'"
            >
              <Key :size="14" :class="svc.is_active ? 'text-success' : 'text-muted-foreground'" />
            </div>

            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-medium text-sm text-foreground">{{ svc.name }}</span>
                <span class="text-xs text-muted-foreground font-mono px-1.5 py-0.5 rounded bg-muted/10">
                  {{ svc.credential_type }}
                </span>
              </div>
              <p v-if="svc.description" class="text-xs text-muted-foreground truncate mt-0.5">
                {{ svc.description }}
              </p>
              <p v-if="svc.env_var_name" class="text-xs text-muted-foreground font-mono mt-0.5">
                {{ svc.env_var_name }}
              </p>
              <p v-if="svc.target_path" class="text-xs text-muted-foreground font-mono mt-0.5">
                {{ svc.target_path }}
              </p>
            </div>

            <!-- Activation toggle -->
            <button
              type="button"
              class="flex items-center gap-1 text-xs px-2.5 py-1 rounded-full border font-medium transition-colors shrink-0"
              :class="
                svc.is_active
                  ? 'border-success/30 bg-success/10 text-success hover:bg-success/20'
                  : 'border-border bg-muted/10 text-muted-foreground hover:bg-muted/20'
              "
              :disabled="toggleLoading === svc.id"
              @click="toggleCredentialServiceActivation(svc)"
            >
              <LoadingSpinner v-if="toggleLoading === svc.id" :size="10" />
              <Check v-else-if="svc.is_active" :size="11" />
              <X v-else :size="11" />
              {{ svc.is_active ? 'Active' : 'Inactive' }}
            </button>
          </div>
        </div>

        <div v-if="credentialServices.length === 0" class="text-center py-12 text-muted-foreground text-sm">
          No credential services found.
        </div>
      </div>
    </template>

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

      <form class="space-y-4" @submit.prevent="createCredentialService">
        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
          <Input
            v-model="serviceName"
            placeholder="GitHub Enterprise"
          />
        </div>

        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Slug</label>
            <Input
              v-model="serviceSlug"
              placeholder="github-enterprise"
              @update:modelValue="serviceSlugTouched = true"
            />
            <p class="text-xs text-muted-foreground mt-1">Used as stable identifier. Auto-generated from name.</p>
          </div>
          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Credential Type</label>
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

        <div v-if="serviceCredentialType === 'env'">
          <label class="text-sm font-medium text-foreground mb-1.5 block">Environment Variable Name</label>
          <Input
            v-model="serviceEnvVarName"
            placeholder="GITHUB_TOKEN"
          />
          <p class="text-xs text-muted-foreground mt-1">
            Must be uppercase snake case, e.g. <code>OPENAI_API_KEY</code>.
          </p>
        </div>

        <div v-else-if="serviceCredentialType === 'file'">
          <label class="text-sm font-medium text-foreground mb-1.5 block">Target Path</label>
          <Input
            v-model="serviceTargetPath"
            placeholder="~/.codex/auth.json"
          />
          <p class="text-xs text-muted-foreground mt-1">
            Supports absolute paths, <code>~/...</code>, <code>${HOME}/...</code>, and relative paths resolved against HOME.
          </p>
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Label</label>
          <Input
            v-model="serviceLabel"
            placeholder="Personal Access Token"
          />
          <p class="text-xs text-muted-foreground mt-1">Optional helper label shown in credential forms.</p>
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Description</label>
          <Input
            v-model="serviceDescription"
            placeholder="Used for repository access and API integrations."
          />
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <Button variant="outline" type="button" :disabled="createServiceLoading" @click="closeCreateCredentialService">
            Cancel
          </Button>
          <Button type="submit" :disabled="!isCreateServiceValid || createServiceLoading">
            <LoadingSpinner v-if="createServiceLoading" :size="12" />
            <Plus v-else :size="12" />
            Create Service
          </Button>
        </div>
      </form>
      </DialogContent>
    </Dialog>

  </div>
</template>
