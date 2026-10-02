<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Info } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useCredentialStore } from '@/stores/credentials'
import { useAuthStore } from '@/stores/auth'

const props = withDefaults(
  defineProps<{ preselectedServiceId?: string; showTrigger?: boolean }>(),
  { showTrigger: true },
)
const emit = defineEmits<{ close: [] }>()
const open = defineModel<boolean>('open', { default: false })
const credentialStore = useCredentialStore()
const authStore = useAuthStore()
const selectedServiceId = ref('')
const name = ref('')
const value = ref('')
const isOrgCredential = ref(false)
const submitting = ref(false)
const contextError = ref<string | null>(null)
const actionError = ref<string | null>(null)

onMounted(async () => {
  if (!credentialStore.services.length) await credentialStore.fetchServices()
})
watch(
  () => authStore.activeOrganizationId,
  () => {
    clearCredentialValues()
    contextError.value = null
    actionError.value = null
    if (props.preselectedServiceId) {
      selectedServiceId.value = props.preselectedServiceId
      if (credentialStore.servicesLoaded) validatePreselectedService()
    }
  },
)
watch(
  () => props.preselectedServiceId,
  (id) => {
    if (!id) return
    if (selectedServiceId.value && selectedServiceId.value !== id) clearCredentialValues()
    selectedServiceId.value = id
    if (credentialStore.servicesLoaded) validatePreselectedService()
  },
  { immediate: true },
)
watch(
  () => [open.value, credentialStore.servicesLoaded] as const,
  ([isOpen]) => {
    if (isOpen && props.preselectedServiceId) {
      if (selectedServiceId.value && selectedServiceId.value !== props.preselectedServiceId)
        clearCredentialValues()
      selectedServiceId.value = props.preselectedServiceId
      validatePreselectedService()
    }
  },
)
const serviceOptions = computed(() =>
  credentialStore.services.filter((service) => service.is_active),
)
const selectedService = computed(() =>
  credentialStore.services.find((service) => service.id === selectedServiceId.value),
)
const isOAuth = computed(() => selectedService.value?.credential_type === 'mcp_oauth')
const isSSHKey = computed(() => selectedService.value?.credential_type === 'ssh_key')
const isFileCredential = computed(() => selectedService.value?.credential_type === 'file')
const isValid = computed(
  () =>
    !!selectedService.value &&
    selectedService.value.is_active &&
    (isOAuth.value || isSSHKey.value || !!value.value.trim()),
)
const defaultName = computed(() =>
  selectedService.value ? `${selectedService.value.name} Credential` : '',
)

function validatePreselectedService(): void {
  const service = credentialStore.services.find((entry) => entry.id === props.preselectedServiceId)
  if (!service && credentialStore.servicesError)
    contextError.value = `Could not load credential services: ${credentialStore.servicesError}`
  else if (!service)
    contextError.value =
      'That credential service is unavailable in this organization. Choose an active service or ask an admin.'
  else if (!service.is_active)
    contextError.value = `${service.name} is inactive for this organization. Ask an admin to activate it before creating credentials.`
  else {
    contextError.value = null
    selectedServiceId.value = service.id
  }
}
function clearCredentialValues(): void {
  name.value = ''
  value.value = ''
  isOrgCredential.value = false
}
function selectService(id: string): void {
  if (selectedServiceId.value !== id) clearCredentialValues()
  selectedServiceId.value = id
  contextError.value = null
  actionError.value = null
}
async function handleSubmit(): Promise<void> {
  if (!isValid.value || !selectedService.value) return
  submitting.value = true
  actionError.value = null
  if (isOAuth.value) {
    const success = await credentialStore.connectOAuthService(
      selectedServiceId.value,
      name.value.trim() || undefined,
      isOrgCredential.value,
    )
    if (!success) {
      actionError.value = 'Unable to start the provider connection. Check access and try again.'
    }
    submitting.value = false
    return
  }
  const success = await credentialStore.createCredential({
    service_id: selectedServiceId.value,
    name: name.value.trim() || undefined,
    value: isSSHKey.value ? undefined : value.value,
    organization_credential: isOrgCredential.value,
  })
  submitting.value = false
  if (success) handleClose()
}
function handleClose(): void {
  open.value = false
  selectedServiceId.value = ''
  clearCredentialValues()
  contextError.value = null
  actionError.value = null
  emit('close')
}
</script>

<template>
  <Dialog v-model:open="open" @update:open="(value) => !value && handleClose()">
    <DialogTrigger v-if="showTrigger" as-child
      ><Button size="sm">Add Credential</Button></DialogTrigger
    >
    <DialogContent>
      <DialogHeader
        ><DialogTitle>{{
          isOAuth ? `Connect ${selectedService?.name || 'OAuth account'}` : 'Add Credential'
        }}</DialogTitle
        ><DialogDescription>{{
          isOAuth
            ? 'A secure provider authorization will open in this tab. OpenCuria never asks for or displays OAuth tokens.'
            : 'Store a credential to be injected into workspaces.'
        }}</DialogDescription></DialogHeader
      >
      <DialogBody>
        <form
          id="create-credential-form"
          class="flex flex-col gap-4"
          @submit.prevent="handleSubmit"
        >
          <div class="space-y-1.5">
            <Label>Service</Label
            ><Select
              :model-value="selectedServiceId"
              :disabled="!!contextError && !selectedServiceId"
              @update:model-value="(value) => selectService(String(value))"
              ><SelectTrigger><SelectValue placeholder="Select an active service" /></SelectTrigger
              ><SelectContent
                ><SelectItem v-for="service in serviceOptions" :key="service.id" :value="service.id"
                  >{{ service.name
                  }}<span v-if="service.credential_type === 'mcp_oauth'"> · OAuth</span></SelectItem
                ></SelectContent
              ></Select
            >
            <p v-if="!serviceOptions.length" class="text-xs text-muted-foreground">
              No active services. Ask an admin to add or activate one.
            </p>
          </div>
          <div
            v-if="contextError"
            role="alert"
            class="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            data-testid="credential-service-context-error"
          >
            {{ contextError }}
          </div>
          <div v-if="selectedServiceId" class="space-y-1.5">
            <Label for="new-credential-name">Name</Label
            ><Input id="new-credential-name" v-model="name" :placeholder="defaultName" />
            <p class="text-xs text-muted-foreground">Optional. Defaults to “{{ defaultName }}”.</p>
          </div>
          <div
            v-if="isOAuth"
            class="flex items-start gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2.5 text-sm"
          >
            <Info :size="16" class="mt-0.5 shrink-0 text-primary" /><span
              >Continue to the provider in this tab to authorize securely. Tokens remain encrypted
              and are never shown or manually entered.</span
            >
          </div>
          <div
            v-else-if="isSSHKey"
            class="flex items-start gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2.5 text-sm"
          >
            <Info :size="16" class="mt-0.5 shrink-0 text-primary" /><span
              >An <strong>Ed25519 SSH key pair</strong> will be generated automatically.</span
            >
          </div>
          <div
            v-if="selectedServiceId && isFileCredential"
            class="flex items-start gap-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-2.5 text-sm"
          >
            <Info :size="16" class="mt-0.5 shrink-0 text-primary" /><span
              >This credential will be written to <strong>{{ selectedService?.target_path }}</strong
              >.</span
            >
          </div>
          <div v-if="selectedServiceId && !isSSHKey && !isOAuth" class="space-y-1.5">
            <Label>Value</Label
            ><Textarea
              v-if="isFileCredential"
              v-model="value"
              :rows="6"
              :placeholder="`Contents for ${selectedService?.target_path}`"
            /><Input
              v-else
              v-model="value"
              type="password"
              :placeholder="`Value for ${selectedService?.env_var_name}`"
            />
            <p class="text-xs text-muted-foreground">Encrypted and never shown again.</p>
          </div>
          <div v-if="authStore.isAdmin" class="flex items-center justify-between gap-3">
            <Label for="create-org-credential" class="font-normal"
              >Share with entire organization</Label
            ><Switch id="create-org-credential" v-model="isOrgCredential" />
          </div>
          <div v-if="actionError" class="text-sm text-destructive" role="alert">
            {{ actionError }}
          </div>
        </form>
      </DialogBody>
      <DialogFooter
        ><Button variant="outline" type="button" :disabled="submitting" @click="handleClose"
          >Cancel</Button
        ><Button
          type="submit"
          form="create-credential-form"
          :disabled="!isValid || submitting || !!contextError"
          >{{
            submitting
              ? isOAuth
                ? 'Connecting…'
                : 'Saving…'
              : isOAuth
                ? 'Connect securely'
                : 'Save Credential'
          }}</Button
        ></DialogFooter
      >
    </DialogContent>
  </Dialog>
</template>
