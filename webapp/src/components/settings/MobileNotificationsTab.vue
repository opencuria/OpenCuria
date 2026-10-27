<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { listCredentials } from '@/services/credentials.api'
import {
  getPushConfiguration,
  savePushConfiguration,
  type PushConfiguration,
} from '@/services/notifications.api'
import { useAutosave } from '@/composables/useAutosave'
import type { Credential } from '@/types'
import { useAuthStore } from '@/stores/auth'
import SettingsSection from './SettingsSection.vue'
import AutosaveStatus from './AutosaveStatus.vue'
import { OPEN_SETTINGS_EVENT } from './settingsTabs'

const auth = useAuthStore()
const loading = ref(true)
const error = ref<string | null>(null)
const credentials = ref<Credential[]>([])
const configuration = ref<PushConfiguration | null>(null)
const apns = ref('none')
const fcm = ref('none')
const selection = computed(() => ({
  apns_credential_id: apns.value === 'none' ? null : apns.value,
  fcm_credential_id: fcm.value === 'none' ? null : fcm.value,
}))
const autosave = useAutosave({
  source: () => selection.value,
  enabled: () => auth.isAdmin && !loading.value && !!configuration.value,
  dirty: () =>
    selection.value.apns_credential_id !== configuration.value?.apns_credential_id ||
    selection.value.fcm_credential_id !== configuration.value?.fcm_credential_id,
  errorMessage: 'Could not save mobile notification settings',
  save: async (isCurrent) => {
    const updated = await savePushConfiguration({ ...selection.value })
    if (isCurrent()) configuration.value = updated
  },
})

async function load(): Promise<void> {
  const scope = auth.activeOrganizationId
  loading.value = true
  error.value = null
  try {
    const [config, rows] = await Promise.all([getPushConfiguration(), listCredentials()])
    if (scope !== auth.activeOrganizationId) return
    configuration.value = config
    credentials.value = rows.filter((row) => row.scope === 'organization')
    apns.value = config.apns_credential_id ?? 'none'
    fcm.value = config.fcm_credential_id ?? 'none'
  } catch (cause) {
    if (scope === auth.activeOrganizationId)
      error.value =
        cause instanceof Error ? cause.message : 'Could not load mobile notification settings'
  } finally {
    if (scope === auth.activeOrganizationId) loading.value = false
  }
}

function manageCredentials(): void {
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: 'credentials' } }))
}
onMounted(() => {
  void load()
})
</script>

<template>
  <div class="space-y-6">
    <LoadingSpinner v-if="loading" :size="24" />
    <div v-else-if="error" role="alert" class="space-y-3 text-sm text-destructive">
      {{ error }}
      <Button variant="outline" @click="load">Retry</Button>
    </div>
    <SettingsSection
      v-else
      title="Mobile notifications"
      description="Send agent completions, failures and requests for input to your team's phones."
    >
      <p class="text-sm text-muted-foreground">
        Add organization credentials for Mobile Push APNS or Mobile Push FCM in Credentials, then
        select them here. Each person enables notifications in the mobile app.
      </p>
      <div class="space-y-2">
        <Label for="mobile-apns">iOS notifications &amp; Live Activities</Label>
        <Select v-model="apns" :disabled="!auth.isAdmin">
          <SelectTrigger id="mobile-apns"
            ><SelectValue placeholder="Choose Apple credentials"
          /></SelectTrigger>
          <SelectContent>
            <SelectItem value="none">Off</SelectItem>
            <SelectItem
              v-for="credential in credentials.filter(
                (row) => row.service_slug === 'mobile-push-apns',
              )"
              :key="credential.id"
              :value="credential.id"
              >{{ credential.name }}</SelectItem
            >
          </SelectContent>
        </Select>
      </div>
      <div class="space-y-2">
        <Label for="mobile-fcm">Android notifications</Label>
        <Select v-model="fcm" :disabled="!auth.isAdmin">
          <SelectTrigger id="mobile-fcm"
            ><SelectValue placeholder="Choose Firebase credentials"
          /></SelectTrigger>
          <SelectContent>
            <SelectItem value="none">Off</SelectItem>
            <SelectItem
              v-for="credential in credentials.filter(
                (row) => row.service_slug === 'mobile-push-fcm',
              )"
              :key="credential.id"
              :value="credential.id"
              >{{ credential.name }}</SelectItem
            >
          </SelectContent>
        </Select>
      </div>
      <AutosaveStatus :state="autosave" />
      <Button variant="outline" @click="manageCredentials">Manage credentials</Button>
    </SettingsSection>
  </div>
</template>
