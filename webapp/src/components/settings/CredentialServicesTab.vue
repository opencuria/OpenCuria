<!--
  CredentialServicesTab — Extrahierter "Credential Services"-Kern aus OrgSettingsView (Schritt 5).

  Enthält die Service-Liste inkl. Create-Dialog und Aktivierungs-Toggle.
  Wird vom Settings-Sheet (Tab "Organization") und weiterhin von
  OrgSettingsView wiederverwendet.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { get, post } from '@/services/api'
import { useAuthStore } from '@/stores/auth'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
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
import { Plus, Key, Check, X } from '@lucide/vue'

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
const activeOrganizationId = computed(() => authStore.activeOrganizationId)

const credentialServices = ref<CredentialServiceWithActivation[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const toggleLoading = ref<string | null>(null)

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

// ---------------------------------------------------------------------------
// Credential service activation toggle
// ---------------------------------------------------------------------------

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
  <div class="space-y-4">
    <div v-if="loading" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <div
      v-else-if="error"
      class="rounded-md border border-error/30 bg-error-muted px-4 py-3 text-sm text-error flex items-center justify-between"
    >
      <span>{{ error }}</span>
      <Button size="icon-sm" variant="ghost" @click="error = null">
        <X :size="14" />
      </Button>
    </div>

    <template v-else>
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
              <span
                class="text-xs text-muted-foreground font-mono px-1.5 py-0.5 rounded bg-muted/10"
              >
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

      <div
        v-if="credentialServices.length === 0"
        class="text-center py-12 text-muted-foreground text-sm"
      >
        No credential services found.
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
            <Input v-model="serviceName" placeholder="GitHub Enterprise" />
          </div>

          <div class="grid gap-3 sm:grid-cols-2">
            <div>
              <label class="text-sm font-medium text-foreground mb-1.5 block">Slug</label>
              <Input
                v-model="serviceSlug"
                placeholder="github-enterprise"
                @update:model-value="serviceSlugTouched = true"
              />
              <p class="text-xs text-muted-foreground mt-1">
                Used as stable identifier. Auto-generated from name.
              </p>
            </div>
            <div>
              <label class="text-sm font-medium text-foreground mb-1.5 block">
                Credential Type
              </label>
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
            <label class="text-sm font-medium text-foreground mb-1.5 block">
              Environment Variable Name
            </label>
            <Input v-model="serviceEnvVarName" placeholder="GITHUB_TOKEN" />
            <p class="text-xs text-muted-foreground mt-1">
              Must be uppercase snake case, e.g. <code>OPENAI_API_KEY</code>.
            </p>
          </div>

          <div v-else-if="serviceCredentialType === 'file'">
            <label class="text-sm font-medium text-foreground mb-1.5 block">Target Path</label>
            <Input v-model="serviceTargetPath" placeholder="~/.codex/auth.json" />
            <p class="text-xs text-muted-foreground mt-1">
              Supports absolute paths, <code>~/...</code>, <code>${HOME}/...</code>, and relative
              paths resolved against HOME.
            </p>
          </div>

          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Label</label>
            <Input v-model="serviceLabel" placeholder="Personal Access Token" />
            <p class="text-xs text-muted-foreground mt-1">
              Optional helper label shown in credential forms.
            </p>
          </div>

          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Description</label>
            <Input
              v-model="serviceDescription"
              placeholder="Used for repository access and API integrations."
            />
          </div>

          <div class="flex justify-end gap-2 pt-2">
            <Button
              variant="outline"
              type="button"
              :disabled="createServiceLoading"
              @click="closeCreateCredentialService"
            >
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
