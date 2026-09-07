<!--
  WorkspacePolicyTab — General settings: automatic workspace stop policy.
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
import type { Organization } from '@/types'
import { formatMinutesAsDuration } from '@/lib/utils'

const authStore = useAuthStore()
const activeOrganizationId = computed(() => authStore.activeOrganizationId)

const loading = ref(true)
const error = ref<string | null>(null)
const organizationSettings = ref<Organization | null>(null)
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

const lastSavedLabel = computed(() => {
  if (organizationSettings.value?.workspace_auto_stop_timeout_minutes != null) {
    return formatMinutesAsDuration(organizationSettings.value.workspace_auto_stop_timeout_minutes)
  }
  return 'Disabled'
})

async function loadData(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    if (!activeOrganizationId.value) {
      throw new Error('No active organization selected')
    }
    organizationSettings.value = await getOrganization(activeOrganizationId.value)
    autoStopEnabled.value = organizationSettings.value.workspace_auto_stop_timeout_minutes != null
    autoStopTimeoutMinutes.value =
      organizationSettings.value.workspace_auto_stop_timeout_minutes ?? 240
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

async function saveWorkspacePolicy(): Promise<void> {
  if (!activeOrganizationId.value || policySaving.value) return
  if (
    autoStopEnabled.value &&
    (!Number.isFinite(autoStopTimeoutMinutes.value) || autoStopTimeoutMinutes.value < 1)
  ) {
    error.value = 'Auto-stop timeout must be at least 1 minute.'
    return
  }

  policySaving.value = true
  error.value = null
  try {
    const updated = await updateOrganizationWorkspacePolicy(activeOrganizationId.value, {
      workspace_auto_stop_timeout_minutes: autoStopEnabled.value
        ? Math.round(autoStopTimeoutMinutes.value)
        : null,
    })
    organizationSettings.value = updated
    autoStopEnabled.value = updated.workspace_auto_stop_timeout_minutes != null
    autoStopTimeoutMinutes.value =
      updated.workspace_auto_stop_timeout_minutes ?? autoStopTimeoutMinutes.value
  } catch (e) {
    error.value = (e as Error).message || 'Failed to update workspace policy'
  } finally {
    policySaving.value = false
  }
}
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
      title="Automatic Workspace Stop"
      :description="
        organizationSettings
          ? `Running workspaces in ${organizationSettings.name} stop automatically after a period without prompts, terminal input, or file interactions.`
          : 'Running workspaces stop automatically after a period without prompts, terminal input, or file interactions.'
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
          <Switch id="workspace-auto-stop" v-model="autoStopEnabled" />
        </div>

        <div v-if="autoStopEnabled" class="space-y-3 border-t border-border px-4 py-4">
          <div class="space-y-2">
            <Label for="auto-stop-minutes">Inactivity timeout (minutes)</Label>
            <Input
              id="auto-stop-minutes"
              :model-value="String(autoStopTimeoutMinutes)"
              type="number"
              min="1"
              step="1"
              @update:model-value="autoStopTimeoutMinutes = Math.max(1, Number($event) || 1)"
            />
          </div>
          <div class="flex flex-wrap gap-2">
            <Button
              v-for="preset in workspacePolicyPresetOptions"
              :key="preset.value"
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
      </div>

      <div class="flex items-center justify-between gap-3">
        <p class="text-xs text-muted-foreground">Last saved: {{ lastSavedLabel }}</p>
        <Button size="sm" :disabled="policySaving" @click="saveWorkspacePolicy">
          <LoadingSpinner v-if="policySaving" :size="12" />
          <span v-else>Save Policy</span>
        </Button>
      </div>
    </SettingsSection>
  </div>
</template>
