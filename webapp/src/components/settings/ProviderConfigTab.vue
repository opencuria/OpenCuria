<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'
import {
  deleteProviderConfig,
  getProviderConfig,
  saveProviderConfig,
  type HarnessProviderConfig,
} from '@/services/harness.api'

const DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1'

const loading = ref(true)
const saving = ref(false)
const deleting = ref(false)
const error = ref<string | null>(null)
const config = ref<HarnessProviderConfig | null>(null)

const apiKey = ref('')
const baseUrl = ref(DEFAULT_BASE_URL)
const defaultModel = ref('')
const smallModel = ref('')
const computerUseModel = ref('')

const apiKeyPlaceholder = computed(() => {
  if (config.value?.has_api_key && config.value.api_key_hint) {
    return `Saved key (${config.value.api_key_hint})`
  }
  if (config.value?.has_api_key) {
    return 'Saved key (leave blank to keep)'
  }
  return 'sk-or-...'
})

function applyConfig(next: HarnessProviderConfig): void {
  config.value = next
  baseUrl.value = next.base_url || DEFAULT_BASE_URL
  defaultModel.value = next.default_model || ''
  smallModel.value = next.small_model || ''
  computerUseModel.value = next.computer_use_model || ''
  apiKey.value = ''
}

async function loadConfig(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    applyConfig(await getProviderConfig())
  } catch (e: unknown) {
    config.value = null
    baseUrl.value = DEFAULT_BASE_URL
    defaultModel.value = ''
    smallModel.value = ''
    computerUseModel.value = ''
    apiKey.value = ''
    const message = e instanceof Error ? e.message : 'Failed to load provider config'
    if (!message.toLowerCase().includes('not found')) {
      error.value = message
    }
  } finally {
    loading.value = false
  }
}

async function handleSave(): Promise<void> {
  if (saving.value) return
  saving.value = true
  error.value = null
  try {
    const saved = await saveProviderConfig({
      api_key: apiKey.value,
      base_url: baseUrl.value.trim() || DEFAULT_BASE_URL,
      default_model: defaultModel.value.trim(),
      small_model: smallModel.value.trim(),
      computer_use_model: computerUseModel.value.trim(),
    })
    applyConfig(saved)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to save provider config'
  } finally {
    saving.value = false
  }
}

async function handleDelete(): Promise<void> {
  if (deleting.value || !config.value) return
  deleting.value = true
  error.value = null
  try {
    await deleteProviderConfig()
    config.value = null
    apiKey.value = ''
    baseUrl.value = DEFAULT_BASE_URL
    defaultModel.value = ''
    smallModel.value = ''
    computerUseModel.value = ''
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to delete provider config'
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  void loadConfig()
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
      title="OpenRouter Provider"
      description="Configure the API key and default models used by the agent harness across all workspaces. The key is encrypted at rest; only a masked hint is shown after saving."
    >
      <div class="grid max-w-xl gap-4">
        <div class="space-y-2">
          <Label for="provider-api-key">API Key</Label>
          <Input
            id="provider-api-key"
            v-model="apiKey"
            type="password"
            autocomplete="off"
            :placeholder="apiKeyPlaceholder"
          />
          <p
            v-if="config?.has_api_key && config.api_key_hint"
            class="text-xs text-muted-foreground"
          >
            Current key: {{ config.api_key_hint }}
          </p>
        </div>

        <div class="space-y-2">
          <Label for="provider-base-url">Base URL</Label>
          <Input
            id="provider-base-url"
            v-model="baseUrl"
            type="url"
            placeholder="https://openrouter.ai/api/v1"
          />
        </div>

        <div class="space-y-2">
          <Label for="provider-default-model">Default Model</Label>
          <Input
            id="provider-default-model"
            v-model="defaultModel"
            placeholder="e.g. anthropic/claude-sonnet-4"
          />
        </div>

        <div class="space-y-2">
          <Label for="provider-small-model">Small Model</Label>
          <Input
            id="provider-small-model"
            v-model="smallModel"
            placeholder="e.g. openai/gpt-4o-mini"
          />
        </div>

        <div class="space-y-2">
          <Label for="provider-computer-use-model">Computer-use model</Label>
          <Input
            id="provider-computer-use-model"
            v-model="computerUseModel"
            placeholder="e.g. anthropic/claude-sonnet-4"
          />
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-2">
        <Button
          v-if="config"
          size="sm"
          variant="ghost"
          class="text-destructive hover:text-destructive"
          :disabled="deleting"
          @click="handleDelete"
        >
          <LoadingSpinner v-if="deleting" :size="12" />
          <span v-else>Delete Config</span>
        </Button>
        <span v-else />
        <Button size="sm" :disabled="saving" @click="handleSave">
          <LoadingSpinner v-if="saving" :size="12" />
          <span v-else>Save Provider Config</span>
        </Button>
      </div>
    </SettingsSection>
  </div>
</template>
