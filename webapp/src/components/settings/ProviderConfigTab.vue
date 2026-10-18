<!-- Provider accounts plus organization-wide default models. -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import ProviderConnectionDialog from './ProviderConnectionDialog.vue'
import ModelPicker from '@/components/common/ModelPicker.vue'
import SettingsRow from './SettingsRow.vue'
import SettingsSection from './SettingsSection.vue'
import { PROVIDER_META, type ProviderMeta, type SettingsProviderId } from './providerMeta'
import type { ProviderModel } from '@/lib/harnessModels'
import { invalidateProviderCatalog, loadProviderModelsCached } from '@/lib/providerCatalog'
import { ApiRequestError } from '@/services/api'
import { useNotificationStore } from '@/stores/notifications'
import { useAuthStore } from '@/stores/auth'
import {
  getProviderConfig,
  listProviderAccounts,
  saveProviderConfig,
  listClaudeModels,
  type HarnessProviderConfig,
  type ProviderAccount,
} from '@/services/harness.api'

const notifications = useNotificationStore()
const authStore = useAuthStore()
const isAdmin = computed(() => authStore.isAdmin)
const organizationId = computed(
  () => `${authStore.user?.id ?? ''}:${authStore.activeOrganizationId ?? ''}`,
)

let loadRevision = 0
let loadStateRevision = 0
const loading = ref(true)
const savingDefaults = ref(false)
const error = ref<string | null>(null)
const config = ref<HarnessProviderConfig | null>(null)
const accounts = ref<ProviderAccount[]>([])
const catalog = ref<ProviderModel[]>([])
const claudeCatalog = ref<ProviderModel[]>([])
const smallModel = ref('')
const smallEffort = ref('')
const activeProvider = ref<SettingsProviderId | null>(null)

const accountsByProvider = computed(() => {
  const map = new Map<SettingsProviderId, ProviderAccount[]>()
  for (const account of accounts.value) {
    const list = map.get(account.provider) ?? []
    list.push(account)
    map.set(account.provider, list)
  }
  return map
})
const activeAccountByProvider = computed(() => {
  const map = new Map<SettingsProviderId, ProviderAccount>()
  for (const account of accounts.value) {
    if (account.is_active) map.set(account.provider, account)
  }
  return map
})
const anyConnected = computed(() =>
  accounts.value.some((account) => account.is_active && account.connected),
)

function currentContextKey(): string {
  return `${authStore.user?.id ?? ''}:${authStore.activeOrganizationId ?? ''}`
}

const modelCountByProvider = computed(() => {
  const counts = new Map<string, number>()
  for (const model of [...catalog.value, ...claudeCatalog.value]) {
    if (!model.provider) continue
    counts.set(model.provider, (counts.get(model.provider) ?? 0) + 1)
  }
  return counts
})
const defaultsDirty = computed(
  () =>
    smallModel.value.trim() !== (config.value?.small_model ?? '') ||
    smallEffort.value.trim() !== (config.value?.small_effort ?? ''),
)

function applyConfig(next: HarnessProviderConfig | null): void {
  config.value = next
  smallModel.value = next?.small_model || ''
  smallEffort.value = next?.small_effort || ''
}

function isNotFoundError(error: unknown): boolean {
  return (
    (error instanceof ApiRequestError && error.status === 404) ||
    (error instanceof Error && error.message.toLowerCase().includes('not found'))
  )
}

async function refreshAll(): Promise<void> {
  const context = currentContextKey()
  const revision = ++loadRevision
  invalidateProviderCatalog()
  const [configRes, accountRes, modelsRes, claudeModels] = await Promise.all([
    getProviderConfig().catch((error: unknown) => {
      if (isNotFoundError(error)) return null
      throw error
    }),
    listProviderAccounts(),
    loadProviderModelsCached().catch(() => [] as ProviderModel[]),
    listClaudeModels().catch(() => [] as ProviderModel[]),
  ])
  if (context !== currentContextKey() || revision !== loadRevision) return
  applyConfig(configRes)
  accounts.value = accountRes
  catalog.value = modelsRes
  claudeCatalog.value = claudeModels
}

async function loadState(): Promise<void> {
  const context = currentContextKey()
  const revision = ++loadStateRevision
  loading.value = true
  error.value = null
  try {
    await refreshAll()
    if (context !== currentContextKey() || revision !== loadStateRevision) return
  } catch (e: unknown) {
    if (context !== currentContextKey() || revision !== loadStateRevision) return
    config.value = null
    accounts.value = []
    catalog.value = []
    claudeCatalog.value = []
    smallModel.value = ''
    smallEffort.value = ''
    const message = e instanceof Error ? e.message : 'Failed to load provider settings'
    if (!message.toLowerCase().includes('not found')) error.value = message
  } finally {
    if (revision === loadStateRevision) loading.value = false
  }
}

function openProviderDialog(provider: SettingsProviderId): void {
  activeProvider.value = provider
}

function rowDetail(meta: ProviderMeta): string {
  const providerAccounts = accountsByProvider.value.get(meta.id) ?? []
  const selected = activeAccountByProvider.value.get(meta.id)
  if (!providerAccounts.length) return meta.description
  if (!selected)
    return `${providerAccounts.length} account${providerAccounts.length === 1 ? '' : 's'} · None selected`
  return `${selected.label} · ${selected.scope === 'organization' ? 'Shared' : 'Personal'}`
}

function handleDialogClosed(open: boolean): void {
  if (!open) activeProvider.value = null
}

async function handleAccountsChanged(): Promise<void> {
  await refreshAll()
}

async function handleSaveDefaults(): Promise<void> {
  if (savingDefaults.value || !defaultsDirty.value) return
  savingDefaults.value = true
  try {
    const saved = await saveProviderConfig({
      small_model: smallModel.value.trim(),
      small_effort: smallEffort.value.trim(),
      default_model: config.value?.default_model ?? '',
      computer_use_model: config.value?.computer_use_model ?? '',
      default_effort: config.value?.default_effort ?? '',
      computer_use_effort: config.value?.computer_use_effort ?? '',
    })
    applyConfig(saved)
    notifications.success('Default models saved')
  } catch (e: unknown) {
    notifications.error('Failed to save default models', e instanceof Error ? e.message : undefined)
  } finally {
    savingDefaults.value = false
  }
}

onMounted(() => void loadState())
watch(organizationId, () => {
  activeProvider.value = null
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
        description="Manage personal and organization-shared accounts. Provider secrets are encrypted at rest."
      >
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <SettingsRow
            v-for="meta in PROVIDER_META"
            :key="meta.id"
            class="transition-colors hover:bg-muted/40"
            :icon-class="
              accountsByProvider.get(meta.id)?.length ? 'bg-success/10 text-success' : undefined
            "
            :data-testid="`provider-row-${meta.id}`"
          >
            <template #icon>
              <component :is="meta.icon" :size="16" aria-hidden="true" />
            </template>
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-medium text-foreground">{{ meta.name }}</span>
                <span
                  v-if="activeAccountByProvider.get(meta.id)?.connected"
                  class="text-xs font-medium text-success"
                  :data-testid="`provider-status-${meta.id}`"
                  >Active</span
                >
              </div>
              <p class="text-sm text-muted-foreground" :data-testid="`provider-detail-${meta.id}`">
                {{ rowDetail(meta) }}
              </p>
            </div>
            <template #badges>
              <span
                v-if="accountsByProvider.get(meta.id)?.length"
                class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
                :data-testid="`provider-account-count-${meta.id}`"
                >{{ accountsByProvider.get(meta.id)?.length }}
                {{ accountsByProvider.get(meta.id)?.length === 1 ? 'account' : 'accounts' }}</span
              >
              <span
                v-if="modelCountByProvider.get(meta.id === 'claude-agent' ? 'claude' : meta.id)"
                class="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
                :data-testid="`provider-model-count-${meta.id}`"
                >{{
                  modelCountByProvider.get(meta.id === 'claude-agent' ? 'claude' : meta.id)
                }}
                models</span
              >
            </template>
            <template #actions>
              <Button
                size="sm"
                :variant="accountsByProvider.get(meta.id)?.length ? 'outline' : 'default'"
                :data-testid="`provider-manage-${meta.id}`"
                @click="openProviderDialog(meta.id)"
                >Manage</Button
              >
            </template>
          </SettingsRow>
        </div>
      </SettingsSection>

      <SettingsSection
        title="Small Model"
        description="Background tasks like session titles. Primary and subagent models are configured under Agents."
      >
        <div
          v-if="!anyConnected"
          class="rounded-md border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground"
          data-testid="defaults-no-provider-hint"
        >
          No provider connected yet. Connect an account above to browse available models — or enter
          provider/model ids manually.
        </div>

        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="provider-small-model" class="block text-sm font-medium"
                >Small Model</Label
              >
              <p class="text-sm text-muted-foreground">
                Background tasks like session titles and compaction.
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ModelPicker
                input-id="provider-small-model"
                v-model:model="smallModel"
                v-model:effort="smallEffort"
                :models="catalog"
                :disabled="savingDefaults"
                variant="field"
                manual-fallback
                allow-default
                default-model-label="None"
                default-option-label="None"
                allow-effort-default
                default-effort-label="Provider default"
                effort-fallback="model-default"
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
      :accounts="activeProvider ? (accountsByProvider.get(activeProvider) ?? []) : []"
      :is-admin="isAdmin"
      @update:open="handleDialogClosed"
      @changed="handleAccountsChanged"
    />
  </div>
</template>
