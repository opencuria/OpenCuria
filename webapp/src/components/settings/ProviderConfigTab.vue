<!--
  ProviderConfigTab — provider connections plus org-wide default models.

  Providers render as SettingsRow list items (design-system list pattern);
  the per-provider connect/manage flow lives in ProviderConnectionDialog.
  Default models use a dirty-tracked save with toast feedback.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import ProviderConnectionDialog from './ProviderConnectionDialog.vue'
import ProviderModelCombobox from './ProviderModelCombobox.vue'
import SettingsRow from './SettingsRow.vue'
import SettingsSection from './SettingsSection.vue'
import { PROVIDER_META, connectionDetail, type ProviderMeta } from './providerMeta'
import type { ProviderId, ProviderModel } from '@/lib/harnessModels'
import { invalidateProviderCatalog, loadProviderModelsCached } from '@/lib/providerCatalog'
import { useNotificationStore } from '@/stores/notifications'
import {
  getProviderConfig,
  listProviderConnections,
  saveProviderConfig,
  type HarnessProviderConfig,
  type ProviderConnection,
} from '@/services/harness.api'

const notifications = useNotificationStore()

const loading = ref(true)
const savingDefaults = ref(false)
const error = ref<string | null>(null)
const config = ref<HarnessProviderConfig | null>(null)
const connections = ref<ProviderConnection[]>([])
const catalog = ref<ProviderModel[]>([])

const defaultModel = ref('')
const smallModel = ref('')
const computerUseModel = ref('')
const defaultEffort = ref('')
const smallEffort = ref('')
const computerUseEffort = ref('')

const activeProvider = ref<ProviderId | null>(null)

const connectionByProvider = computed(() => {
  const map = new Map<ProviderId, ProviderConnection>()
  for (const row of connections.value) {
    map.set(row.provider, row)
  }
  return map
})

const activeConnection = computed(() =>
  activeProvider.value ? connectionByProvider.value.get(activeProvider.value) : undefined,
)

const anyConnected = computed(() => connections.value.some((row) => row.connected))

/** Number of catalog models per provider, once the catalog is loaded. */
const modelCountByProvider = computed(() => {
  const counts = new Map<string, number>()
  for (const model of catalog.value) {
    if (!model.provider) continue
    counts.set(model.provider, (counts.get(model.provider) ?? 0) + 1)
  }
  return counts
})

const defaultsDirty = computed(
  () =>
    defaultModel.value.trim() !== (config.value?.default_model ?? '') ||
    smallModel.value.trim() !== (config.value?.small_model ?? '') ||
    computerUseModel.value.trim() !== (config.value?.computer_use_model ?? '') ||
    defaultEffort.value.trim() !== (config.value?.default_effort ?? '') ||
    smallEffort.value.trim() !== (config.value?.small_effort ?? '') ||
    computerUseEffort.value.trim() !== (config.value?.computer_use_effort ?? ''),
)

function applyConfig(next: HarnessProviderConfig): void {
  config.value = next
  defaultModel.value = next.default_model || ''
  smallModel.value = next.small_model || ''
  computerUseModel.value = next.computer_use_model || ''
  defaultEffort.value = next.default_effort || ''
  smallEffort.value = next.small_effort || ''
  computerUseEffort.value = next.computer_use_effort || ''
}

async function refreshAll(): Promise<void> {
  invalidateProviderCatalog()
  const [configRes, connectionRes, modelsRes] = await Promise.all([
    getProviderConfig(),
    listProviderConnections(),
    loadProviderModelsCached().catch(() => [] as ProviderModel[]),
  ])
  applyConfig(configRes)
  connections.value = connectionRes
  catalog.value = modelsRes
}

async function loadState(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    await refreshAll()
  } catch (e: unknown) {
    config.value = null
    connections.value = []
    catalog.value = []
    defaultModel.value = ''
    smallModel.value = ''
    computerUseModel.value = ''
    defaultEffort.value = ''
    smallEffort.value = ''
    computerUseEffort.value = ''
    const message = e instanceof Error ? e.message : 'Failed to load provider settings'
    if (!message.toLowerCase().includes('not found')) {
      error.value = message
    }
  } finally {
    loading.value = false
  }
}

function openProviderDialog(provider: ProviderId): void {
  activeProvider.value = provider
}

/** Row subtitle: connection summary when connected, otherwise the pitch. */
function rowDetail(meta: ProviderMeta): string {
  const connection = connectionByProvider.value.get(meta.id)
  if (!connection?.connected) return meta.description
  return connectionDetail(connection)
}

function handleDialogClosed(open: boolean): void {
  if (!open) activeProvider.value = null
}

/** Connection saved or disconnected: refresh state and close the dialog. */
async function handleConnectionChanged(): Promise<void> {
  activeProvider.value = null
  await refreshAll()
}

/** ChatGPT OAuth completed: refresh state, keep the dialog open. */
async function handleConnectionConnected(): Promise<void> {
  await refreshAll()
}

async function handleSaveDefaults(): Promise<void> {
  if (savingDefaults.value || !defaultsDirty.value) return
  savingDefaults.value = true
  try {
    const saved = await saveProviderConfig({
      default_model: defaultModel.value.trim(),
      small_model: smallModel.value.trim(),
      computer_use_model: computerUseModel.value.trim(),
      default_effort: defaultEffort.value.trim(),
      small_effort: smallEffort.value.trim(),
      computer_use_effort: computerUseEffort.value.trim(),
    })
    applyConfig(saved)
    notifications.success('Default models saved')
  } catch (e: unknown) {
    notifications.error('Failed to save default models', e instanceof Error ? e.message : undefined)
  } finally {
    savingDefaults.value = false
  }
}

onMounted(() => {
  void loadState()
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

    <template v-else>
      <SettingsSection
        title="Providers"
        description="Connect one or more model providers for your organization. Credentials are encrypted at rest."
      >
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <SettingsRow
            v-for="meta in PROVIDER_META"
            :key="meta.id"
            class="cursor-pointer transition-colors hover:bg-muted/40"
            :icon-class="
              connectionByProvider.get(meta.id)?.connected
                ? 'bg-success/10 text-success'
                : undefined
            "
            :data-testid="`provider-row-${meta.id}`"
            @click="openProviderDialog(meta.id)"
          >
            <template #icon>
              <component :is="meta.icon" :size="16" aria-hidden="true" />
            </template>
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-medium text-foreground">{{ meta.name }}</span>
                <span
                  v-if="connectionByProvider.get(meta.id)?.connected"
                  class="text-xs font-medium text-success"
                  :data-testid="`provider-status-${meta.id}`"
                >
                  Connected
                </span>
              </div>
              <p class="text-sm text-muted-foreground" :data-testid="`provider-detail-${meta.id}`">
                {{ rowDetail(meta) }}
              </p>
            </div>
            <template #badges>
              <span
                v-if="modelCountByProvider.get(meta.id)"
                class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
                :data-testid="`provider-model-count-${meta.id}`"
              >
                {{ modelCountByProvider.get(meta.id) }} models
              </span>
            </template>
            <template #actions>
              <Button
                size="sm"
                :variant="connectionByProvider.get(meta.id)?.connected ? 'outline' : 'default'"
                :data-testid="`provider-manage-${meta.id}`"
                @click.stop="openProviderDialog(meta.id)"
              >
                {{ connectionByProvider.get(meta.id)?.connected ? 'Manage' : 'Connect' }}
              </Button>
            </template>
          </SettingsRow>
        </div>
      </SettingsSection>

      <SettingsSection
        title="Default Models"
        description="Org-wide defaults for new sessions. Pick a model from any connected provider or enter a provider/model id manually."
      >
        <div
          v-if="!anyConnected"
          class="rounded-md border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground"
          data-testid="defaults-no-provider-hint"
        >
          No provider connected yet. Connect a provider above to browse available models — or enter
          provider/model ids manually.
        </div>

        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="provider-default-model" class="block text-sm font-medium">
                Default Model
              </Label>
              <p class="text-sm text-muted-foreground">Primary model for new chat sessions.</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ProviderModelCombobox
                input-id="provider-default-model"
                v-model="defaultModel"
                :effort="defaultEffort"
                :models="catalog"
                empty-hint="Connect a provider to browse models, or enter a provider/model id manually."
                @update:effort="defaultEffort = $event"
              />
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="provider-small-model" class="block text-sm font-medium">
                Small Model
              </Label>
              <p class="text-sm text-muted-foreground">
                Background tasks like session titles and compaction.
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ProviderModelCombobox
                input-id="provider-small-model"
                v-model="smallModel"
                :effort="smallEffort"
                :models="catalog"
                empty-hint="Connect a provider to browse models, or enter a provider/model id manually."
                @update:effort="smallEffort = $event"
              />
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="provider-computer-use-model" class="block text-sm font-medium">
                Computer-use Model
              </Label>
              <p class="text-sm text-muted-foreground">
                Desktop automation with the computer-use agent.
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ProviderModelCombobox
                input-id="provider-computer-use-model"
                v-model="computerUseModel"
                :effort="computerUseEffort"
                :models="catalog"
                empty-hint="Connect a provider to browse models, or enter a provider/model id manually."
                @update:effort="computerUseEffort = $event"
              />
            </div>
          </div>
        </div>

        <div class="flex items-center justify-between gap-3">
          <p class="text-xs text-muted-foreground" data-testid="defaults-status">
            {{ defaultsDirty ? 'Unsaved changes' : 'All changes saved' }}
          </p>
          <Button
            size="sm"
            :disabled="savingDefaults || !defaultsDirty"
            data-testid="save-default-models"
            @click="handleSaveDefaults"
          >
            <LoadingSpinner v-if="savingDefaults" :size="12" />
            <span v-else>Save Defaults</span>
          </Button>
        </div>
      </SettingsSection>
    </template>

    <ProviderConnectionDialog
      :open="activeProvider !== null"
      :provider="activeProvider"
      :connection="activeConnection"
      @update:open="handleDialogClosed"
      @changed="handleConnectionChanged"
      @connected="handleConnectionConnected"
    />
  </div>
</template>
