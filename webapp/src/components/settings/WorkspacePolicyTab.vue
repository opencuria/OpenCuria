<!--
  WorkspacePolicyTab — General settings: automatic workspace stop and image version retention.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getOrganization, updateOrganizationWorkspacePolicy } from '@/services/organizations.api'
import { useAuthStore } from '@/stores/auth'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'
import { useAutosave } from '@/composables/useAutosave'
import AutosaveStatus from './AutosaveStatus.vue'
import type { Organization } from '@/types'
import { formatMinutesAsDuration } from '@/lib/utils'

const authStore = useAuthStore()
const activeOrganizationId = computed(() => authStore.activeOrganizationId)
const canEdit = computed(() => authStore.isAdmin)

const loading = ref(true)
const error = ref<string | null>(null)
const organizationSettings = ref<Organization | null>(null)
const autoStopEnabled = ref(false)
const autoStopTimeoutMinutes = ref<string | number>(240)
const imageVersionsToKeep = ref<string | number>(2)
const MAX_IMAGE_VERSIONS_TO_KEEP = 20

const workspacePolicyPresetOptions = [
  { value: 30, label: '30 min' },
  { value: 60, label: '1h' },
  { value: 240, label: '4h' },
  { value: 480, label: '8h' },
  { value: 1440, label: '24h' },
]

const workspacePolicySummary = computed(() =>
  autoStopEnabled.value
    ? `Inactive workspaces stop after ${formatMinutesAsDuration(Number(autoStopTimeoutMinutes.value))}.`
    : 'Inactive workspaces keep running until someone stops them.',
)

const policy = computed(() => ({
  workspace_auto_stop_timeout_minutes: autoStopEnabled.value
    ? Number(autoStopTimeoutMinutes.value)
    : null,
  image_versions_to_keep: Number(imageVersionsToKeep.value),
}))
const validationError = computed(() => {
  if (
    autoStopEnabled.value &&
    (!String(autoStopTimeoutMinutes.value).trim() ||
      !Number.isSafeInteger(Number(autoStopTimeoutMinutes.value)) ||
      Number(autoStopTimeoutMinutes.value) < 1 ||
      Number(autoStopTimeoutMinutes.value) > 10080)
  )
    return 'Enter a whole number of minutes between 1 and 10080.'
  if (
    !String(imageVersionsToKeep.value).trim() ||
    !Number.isInteger(Number(imageVersionsToKeep.value)) ||
    Number(imageVersionsToKeep.value) < 1 ||
    Number(imageVersionsToKeep.value) > MAX_IMAGE_VERSIONS_TO_KEEP
  )
    return 'Enter an image version count between 1 and 20.'
  return ''
})
const autosave = useAutosave({
  source: () => [autoStopEnabled.value, autoStopTimeoutMinutes.value, imageVersionsToKeep.value],
  dirty: () =>
    policy.value.workspace_auto_stop_timeout_minutes !==
      organizationSettings.value?.workspace_auto_stop_timeout_minutes ||
    policy.value.image_versions_to_keep !==
      (organizationSettings.value?.image_versions_to_keep ?? 2),
  enabled: () => canEdit.value && !loading.value && !!organizationSettings.value && !error.value,
  valid: () => !validationError.value,
  errorMessage: 'Failed to update workspace policy',
  save: async (isCurrent) => {
    const updated = await updateOrganizationWorkspacePolicy(activeOrganizationId.value!, {
      ...policy.value,
    })
    if (isCurrent()) organizationSettings.value = updated
  },
})

async function loadData(): Promise<void> {
  const organizationId = activeOrganizationId.value
  loading.value = true
  error.value = null
  try {
    if (!activeOrganizationId.value) {
      throw new Error('No active organization selected')
    }
    const next = await getOrganization(organizationId!)
    if (organizationId !== activeOrganizationId.value) return
    organizationSettings.value = next
    autoStopEnabled.value = organizationSettings.value.workspace_auto_stop_timeout_minutes != null
    autoStopTimeoutMinutes.value =
      organizationSettings.value.workspace_auto_stop_timeout_minutes ?? 240
    imageVersionsToKeep.value = organizationSettings.value.image_versions_to_keep ?? 2
  } catch (e: unknown) {
    if (organizationId !== activeOrganizationId.value) return
    error.value = (e as Error).message || 'Failed to load settings'
  } finally {
    if (organizationId === activeOrganizationId.value) loading.value = false
  }
}

onMounted(() => {
  void loadData()
})

watch(activeOrganizationId, () => {
  void loadData()
})
</script>

<template>
  <div class="space-y-6">
    <div v-if="loading" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <div
      v-else-if="error"
      class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
    >
      {{ error }}
    </div>

    <SettingsSection
      v-else
      title="Workspace Policy"
      :description="
        organizationSettings
          ? `Applies to all workspaces and images in ${organizationSettings.name}. Inactivity means no prompts, terminal input, or file interactions.`
          : 'Applies to all workspaces and images. Inactivity means no prompts, terminal input, or file interactions.'
      "
    >
      <div class="overflow-hidden rounded-lg border border-border bg-card">
        <div class="flex items-center justify-between gap-4 px-4 py-4">
          <label for="workspace-auto-stop" class="min-w-0 flex-1 cursor-pointer space-y-1">
            <div class="text-sm font-medium text-foreground">
              Stop inactive workspaces
            </div>
            <p class="text-sm text-muted-foreground">{{ workspacePolicySummary }}</p>
          </label>
          <Switch id="workspace-auto-stop" v-model="autoStopEnabled" :disabled="!canEdit" />
        </div>

        <div v-if="autoStopEnabled" class="space-y-3 border-t border-border px-4 py-4">
          <div class="space-y-2">
            <Label for="auto-stop-minutes">Inactivity timeout (minutes)</Label>
            <Input
              id="auto-stop-minutes"
              :disabled="!canEdit"
              :model-value="String(autoStopTimeoutMinutes)"
              type="number"
              min="1"
              max="10080"
              step="1"
              @update:model-value="autoStopTimeoutMinutes = $event"
            />
          </div>
          <div class="flex flex-wrap gap-2">
            <Button
              v-for="preset in workspacePolicyPresetOptions"
              :key="preset.value"
              :disabled="!canEdit"
              size="sm"
              variant="outline"
              type="button"
              @click="autoStopTimeoutMinutes = preset.value"
            >
              {{ preset.label }}
            </Button>
          </div>
          <p class="text-sm text-muted-foreground">
            Active prompt sessions prevent auto-stop until they finish.
          </p>
        </div>

        <div class="flex items-center justify-between gap-4 border-t border-border px-4 py-4">
          <label for="image-versions-to-keep" class="min-w-0 flex-1 space-y-1">
            <div class="text-sm font-medium text-foreground">Image versions to keep</div>
            <p class="text-sm text-muted-foreground">
              Older versions are deleted automatically once no workspace uses them.
            </p>
          </label>
          <Input
            id="image-versions-to-keep"
            :disabled="!canEdit"
            class="w-20"
            data-testid="image-versions-to-keep"
            :model-value="String(imageVersionsToKeep)"
            type="number"
            min="1"
            :max="MAX_IMAGE_VERSIONS_TO_KEEP"
            step="1"
            @update:model-value="imageVersionsToKeep = $event"
          />
        </div>
      </div>

      <p v-if="validationError" class="text-xs text-destructive" role="alert">{{ validationError }}</p>
      <AutosaveStatus :state="autosave" data-testid="workspace-policy-status" />
    </SettingsSection>
  </div>
</template>
