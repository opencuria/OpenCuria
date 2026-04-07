<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { UiDialog, UiInput, UiButton, UiSelect } from '@/components/ui'
import { useCredentialStore } from '@/stores/credentials'
import { useAuthStore } from '@/stores/auth'
import type { CredentialService } from '@/types'
import { Info } from 'lucide-vue-next'

const credentialStore = useCredentialStore()
const authStore = useAuthStore()

const open = ref(false)
const selectedServiceId = ref('')
const name = ref('')
const value = ref('')
const isOrgCredential = ref(false)
const submitting = ref(false)

// Fetch services on mount
onMounted(async () => {
  if (!credentialStore.services.length) {
    await credentialStore.fetchServices()
  }
})

const serviceOptions = computed(() =>
  credentialStore.services.map((s: CredentialService) => ({
    value: s.id,
    label: s.name,
  })),
)

const selectedService = computed(() =>
  credentialStore.services.find((s: CredentialService) => s.id === selectedServiceId.value),
)

const isSSHKey = computed(() => selectedService.value?.credential_type === 'ssh_key')

const defaultName = computed(() => {
  if (selectedService.value) return `${selectedService.value.name} Credential`
  return ''
})

const isValid = computed(() => {
  if (!selectedServiceId.value) return false
  if (isSSHKey.value) return true
  return value.value.trim().length > 0
})

async function handleSubmit(): Promise<void> {
  if (!isValid.value) return

  submitting.value = true
  const success = await credentialStore.createCredential({
    service_id: selectedServiceId.value,
    name: name.value.trim() || undefined,
    value: isSSHKey.value ? undefined : value.value,
    organization_credential: isOrgCredential.value,
  })
  submitting.value = false

  if (success) {
    handleClose()
  }
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    selectedServiceId.value = ''
    name.value = ''
    value.value = ''
    isOrgCredential.value = false
  }, 200)
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Add Credential"
    description="Store a credential to be injected into workspaces."
    @update:open="(v) => (v ? (open = true) : handleClose())"
  >
    <template #trigger>
      <UiButton @click="open = true">Add Credential</UiButton>
    </template>

    <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <!-- Service selection -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Service</label>
        <UiSelect
          v-model="selectedServiceId"
          :options="serviceOptions"
          placeholder="Select a service"
        />
        <p v-if="!serviceOptions.length" class="text-xs text-muted-fg mt-1">
          No services configured. Ask an admin to add services via the admin panel.
        </p>
      </div>

      <!-- Credential name -->
      <div v-if="selectedServiceId">
        <label class="text-sm font-medium text-fg mb-1.5 block">Name</label>
        <UiInput
          v-model="name"
          :placeholder="defaultName"
        />
        <p class="text-xs text-muted-fg mt-1">
          Optional. Defaults to "{{ defaultName }}".
        </p>
      </div>

      <!-- SSH Key info -->
      <div v-if="isSSHKey" class="flex items-start gap-2 rounded-[var(--radius-md)] border border-primary/30 bg-primary/5 px-3 py-2.5 text-sm text-fg">
        <Info :size="16" class="mt-0.5 shrink-0 text-primary" />
        <span>An <strong>Ed25519 SSH key pair</strong> will be generated automatically. You can view the public key after creation.</span>
      </div>

      <!-- Credential value (non-SSH only) -->
      <div v-if="selectedServiceId && !isSSHKey">
        <label class="text-sm font-medium text-fg mb-1.5 block">Value</label>
        <UiInput
          v-model="value"
          type="password"
          :placeholder="selectedService ? `Value for ${selectedService.env_var_name}` : 'Credential value'"
        />
        <p class="text-xs text-muted-fg mt-1">
          This value will be encrypted and stored securely. It is never shown again.
        </p>
      </div>

      <!-- Organization toggle (admins only) -->
      <div v-if="authStore.isAdmin" class="flex items-center gap-2">
        <input
          id="create-org-credential"
          v-model="isOrgCredential"
          type="checkbox"
          class="rounded border-border"
        />
        <label for="create-org-credential" class="text-sm text-fg cursor-pointer">
          Share with entire organization
        </label>
      </div>

      <!-- Actions -->
      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" @click="handleClose">Cancel</UiButton>
        <UiButton type="submit" :disabled="!isValid || submitting">
          {{ submitting ? 'Saving…' : 'Save Credential' }}
        </UiButton>
      </div>
    </form>
  </UiDialog>
</template>
