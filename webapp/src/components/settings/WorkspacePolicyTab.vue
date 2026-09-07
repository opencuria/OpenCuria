<!--
  WorkspacePolicyTab — Extrahierter "Allgemein"-Kern aus OrgSettingsView (Schritt 5).

  Enthält nur die Workspace-Policy-Section (Auto-Stop); der Page-Header und die
  Tab-Leiste bleiben in der View. Wird vom Settings-Sheet (Tab "Allgemein")
  und weiterhin von OrgSettingsView wiederverwendet.
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { getOrganization, updateOrganizationWorkspacePolicy } from '@/services/organizations.api'
import { useAuthStore } from '@/stores/auth'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
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
  <div class="space-y-4">
    <p class="text-sm text-muted-foreground">
      Configure automatic workspace stop for the active organization
      <template v-if="organizationSettings">({{ organizationSettings.name }})</template>.
    </p>

    <div v-if="loading" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <div
      v-else-if="error"
      class="rounded-md border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ error }}
    </div>

    <div v-else class="rounded-lg border border-border bg-card p-5">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h3 class="text-base font-semibold text-foreground">Automatic Workspace Stop</h3>
          <p class="mt-1 text-sm text-muted-foreground">
            Running workspaces stop automatically after the configured period without prompts,
            terminal input, or file interactions.
          </p>
        </div>
        <Badge variant="secondary">
          {{ autoStopEnabled ? 'Enabled' : 'Disabled' }}
        </Badge>
      </div>

      <div class="mt-5 space-y-4">
        <label
          class="flex items-center justify-between gap-4 rounded-md border border-border px-4 py-3"
        >
          <div>
            <div class="text-sm font-medium text-foreground">
              Automatically stop inactive workspaces
            </div>
            <div class="text-xs text-muted-foreground mt-0.5">
              This policy applies to every workspace in the active organization.
            </div>
          </div>
          <input v-model="autoStopEnabled" type="checkbox" class="h-4 w-4 accent-primary" />
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
                @update:model-value="autoStopTimeoutMinutes = Math.max(1, Number($event) || 1)"
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
            <div class="text-xs uppercase tracking-[0.16em] text-muted-foreground">
              Policy Summary
            </div>
            <div class="mt-2 text-sm text-foreground">{{ workspacePolicySummary }}</div>
            <div class="mt-2 text-xs text-muted-foreground">
              Active prompt sessions prevent auto-stop until they finish.
            </div>
          </div>
        </div>

        <div
          v-else
          class="rounded-md border border-border bg-muted/10 px-4 py-3 text-sm text-muted-foreground"
        >
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
</template>
