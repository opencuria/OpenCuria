<!--
  ProviderConnectionDialog — connect/manage/disconnect a single provider.

  Owns the OpenRouter form, the generic OpenAI-compatible endpoint form
  (base URL + optional API key + manual model list, one id per line),
  the ChatGPT device-code OAuth flow (polling) and the Bedrock credential
  form. Secrets are never echoed back: key inputs stay blank and a blank
  value keeps the stored secret on save. Emits `changed` after
  save/disconnect (parent refreshes and closes) and `connected` when the
  ChatGPT OAuth flow completes (parent refreshes, dialog stays open).
  Backend errors surface inline via `dialogError`.
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Copy, ExternalLink, Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { connectionDetail, providerMeta } from './providerMeta'
import type { ProviderId } from '@/lib/harnessModels'
import {
  cancelChatGptOAuth,
  deleteProviderConnection,
  getChatGptOAuthStatus,
  saveProviderConnection,
  startChatGptOAuth,
  type ProviderConnection,
} from '@/services/harness.api'

const DEFAULT_OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
const DEFAULT_BEDROCK_REGION = 'us-east-1'

const props = defineProps<{
  provider: ProviderId | null
  connection?: ProviderConnection
}>()

const open = defineModel<boolean>('open', { default: false })

const emit = defineEmits<{
  changed: []
  connected: []
}>()

const meta = computed(() => providerMeta(props.provider))

const saving = ref(false)
const dialogError = ref<string | null>(null)
const disconnectConfirm = ref(false)

const openRouterApiKey = ref('')
const openRouterBaseUrl = ref(DEFAULT_OPENROUTER_BASE_URL)

const compatApiKey = ref('')
const compatBaseUrl = ref('')
const compatModelsText = ref('')

const bedrockAuthMethod = ref<'access_keys' | 'bearer'>('access_keys')
const bedrockRegion = ref(DEFAULT_BEDROCK_REGION)
const bedrockAccessKeyId = ref('')
const bedrockSecretAccessKey = ref('')
const bedrockSessionToken = ref('')
const bedrockBearerToken = ref('')

type ChatGptOAuthPhase = 'idle' | 'pending' | 'connected' | 'expired' | 'denied' | 'error'

const chatGptOAuthPhase = ref<ChatGptOAuthPhase>('idle')
const chatGptUserCode = ref('')
const chatGptVerificationUrl = ref('')
const chatGptAccountId = ref('')
const chatGptOAuthError = ref<string | null>(null)
let chatGptPollTimer: ReturnType<typeof setTimeout> | null = null
let chatGptPollIntervalSec = 5

const isConnected = computed(() =>
  props.provider === 'chatgpt'
    ? Boolean(props.connection?.connected) || chatGptOAuthPhase.value === 'connected'
    : Boolean(props.connection?.connected),
)

const statusDetail = computed(() => {
  if (props.provider === 'chatgpt' && chatGptAccountId.value) {
    return `Account ${chatGptAccountId.value}`
  }
  return props.connection ? connectionDetail(props.connection) : ''
})

const openRouterApiKeyPlaceholder = computed(() => {
  const hint = props.connection?.api_key_hint
  if (hint) return `Saved key (${hint})`
  return 'sk-or-…'
})

const compatApiKeyPlaceholder = computed(() => {
  const hint = props.connection?.api_key_hint
  if (hint) return `Saved key (${hint})`
  return 'Optional API key'
})

/** Trim, drop empties, dedupe (order-preserving); empty list stays empty. */
function parseCompatModels(text: string): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const line of text.split('\n')) {
    const item = line.trim()
    if (!item || seen.has(item)) continue
    seen.add(item)
    out.push(item)
  }
  return out
}

function resetForms(): void {
  dialogError.value = null
  disconnectConfirm.value = false
  openRouterApiKey.value = ''
  openRouterBaseUrl.value = props.connection?.base_url || DEFAULT_OPENROUTER_BASE_URL
  compatApiKey.value = ''
  compatBaseUrl.value = props.connection?.base_url || ''
  compatModelsText.value = (props.connection?.models ?? []).join('\n')
  bedrockAuthMethod.value = props.connection?.auth_method === 'bearer' ? 'bearer' : 'access_keys'
  bedrockRegion.value = props.connection?.region || DEFAULT_BEDROCK_REGION
  bedrockAccessKeyId.value = ''
  bedrockSecretAccessKey.value = ''
  bedrockSessionToken.value = ''
  bedrockBearerToken.value = ''
  chatGptOAuthError.value = null
  if (props.connection?.connected) {
    chatGptOAuthPhase.value = 'connected'
    chatGptAccountId.value = props.connection.account_id || ''
  } else {
    chatGptOAuthPhase.value = 'idle'
    chatGptUserCode.value = ''
    chatGptVerificationUrl.value = ''
    chatGptAccountId.value = ''
  }
}

function stopChatGptPolling(): void {
  if (chatGptPollTimer) {
    clearTimeout(chatGptPollTimer)
    chatGptPollTimer = null
  }
}

async function pollChatGptOAuthOnce(): Promise<void> {
  try {
    const status = await getChatGptOAuthStatus()
    if (status.status === 'pending') {
      scheduleChatGptPoll()
      return
    }
    stopChatGptPolling()
    if (status.status === 'connected') {
      chatGptOAuthPhase.value = 'connected'
      chatGptAccountId.value = status.account_id || ''
      emit('connected')
      return
    }
    if (status.status === 'denied') {
      chatGptOAuthPhase.value = 'denied'
      chatGptOAuthError.value = 'Authorization was denied. Try again when ready.'
      return
    }
    if (status.status === 'expired' || status.status === 'no_flow') {
      chatGptOAuthPhase.value = 'expired'
      chatGptOAuthError.value = 'The authorization code expired. Start a new connection.'
    }
  } catch (e: unknown) {
    stopChatGptPolling()
    chatGptOAuthPhase.value = 'error'
    chatGptOAuthError.value = e instanceof Error ? e.message : 'OAuth polling failed'
  }
}

function scheduleChatGptPoll(): void {
  stopChatGptPolling()
  chatGptPollTimer = setTimeout(() => {
    void pollChatGptOAuthOnce()
  }, chatGptPollIntervalSec * 1000)
}

async function startChatGptConnect(): Promise<void> {
  dialogError.value = null
  chatGptOAuthError.value = null
  saving.value = true
  try {
    const flow = await startChatGptOAuth()
    chatGptUserCode.value = flow.user_code
    chatGptVerificationUrl.value = flow.verification_url
    chatGptPollIntervalSec = Math.max(1, flow.interval)
    chatGptOAuthPhase.value = 'pending'
    scheduleChatGptPoll()
  } catch (e: unknown) {
    chatGptOAuthPhase.value = 'error'
    chatGptOAuthError.value = e instanceof Error ? e.message : 'Failed to start OAuth'
  } finally {
    saving.value = false
  }
}

async function copyChatGptUserCode(): Promise<void> {
  if (!chatGptUserCode.value) return
  try {
    await navigator.clipboard.writeText(chatGptUserCode.value)
  } catch {
    // Clipboard may be unavailable in tests.
  }
}

async function saveOpenRouter(): Promise<void> {
  saving.value = true
  dialogError.value = null
  try {
    await saveProviderConnection('openrouter', {
      api_key: openRouterApiKey.value,
      base_url: openRouterBaseUrl.value.trim() || DEFAULT_OPENROUTER_BASE_URL,
    })
    emit('changed')
  } catch (e: unknown) {
    dialogError.value = e instanceof Error ? e.message : 'Failed to save OpenRouter connection'
  } finally {
    saving.value = false
  }
}

async function saveCompat(): Promise<void> {
  saving.value = true
  dialogError.value = null
  try {
    const baseUrl = compatBaseUrl.value.trim()
    if (!baseUrl) {
      dialogError.value = 'Base URL is required.'
      return
    }
    await saveProviderConnection('openai-compatible', {
      api_key: compatApiKey.value,
      base_url: baseUrl,
      models: parseCompatModels(compatModelsText.value),
    })
    emit('changed')
  } catch (e: unknown) {
    dialogError.value =
      e instanceof Error ? e.message : 'Failed to save OpenAI Compatible connection'
  } finally {
    saving.value = false
  }
}

async function saveBedrock(): Promise<void> {
  saving.value = true
  dialogError.value = null
  try {
    const payload =
      bedrockAuthMethod.value === 'bearer'
        ? {
            auth_method: 'bearer',
            region: bedrockRegion.value.trim() || DEFAULT_BEDROCK_REGION,
            bearer_token: bedrockBearerToken.value,
          }
        : {
            auth_method: 'access_keys',
            region: bedrockRegion.value.trim() || DEFAULT_BEDROCK_REGION,
            access_key_id: bedrockAccessKeyId.value,
            secret_access_key: bedrockSecretAccessKey.value,
            session_token: bedrockSessionToken.value,
          }
    await saveProviderConnection('amazon-bedrock', payload)
    emit('changed')
  } catch (e: unknown) {
    dialogError.value = e instanceof Error ? e.message : 'Failed to save Bedrock connection'
  } finally {
    saving.value = false
  }
}

async function disconnectProvider(): Promise<void> {
  if (!props.provider) return
  saving.value = true
  dialogError.value = null
  try {
    await deleteProviderConnection(props.provider)
    emit('changed')
  } catch (e: unknown) {
    dialogError.value = e instanceof Error ? e.message : 'Failed to disconnect provider'
  } finally {
    saving.value = false
    disconnectConfirm.value = false
  }
}

watch(
  () => [open.value, props.provider] as const,
  ([isOpen], previous) => {
    if (isOpen) {
      resetForms()
      return
    }
    // previous is undefined on the immediate run.
    if (previous?.[0]) {
      // Best-effort cancel when the dialog closes mid-OAuth-flow.
      if (props.provider === 'chatgpt' && chatGptOAuthPhase.value === 'pending') {
        void cancelChatGptOAuth().catch(() => {})
      }
      stopChatGptPolling()
      disconnectConfirm.value = false
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  stopChatGptPolling()
})
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="sm:max-w-md">
      <DialogHeader v-if="meta">
        <div class="flex items-center gap-3">
          <div
            class="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground"
          >
            <component :is="meta.icon" :size="16" aria-hidden="true" />
          </div>
          <div class="min-w-0 space-y-1">
            <DialogTitle>{{ meta.name }}</DialogTitle>
            <DialogDescription>{{ meta.dialogDescription }}</DialogDescription>
          </div>
        </div>
      </DialogHeader>

      <DialogBody v-if="meta" class="space-y-4">
        <p v-if="dialogError" class="text-sm text-destructive">{{ dialogError }}</p>

        <div
          v-if="isConnected"
          class="flex items-start gap-3 rounded-md border border-border bg-muted/40 p-4"
          data-testid="connection-status"
        >
          <span class="mt-1.5 size-2 shrink-0 rounded-full bg-success" aria-hidden="true" />
          <div class="min-w-0 space-y-0.5">
            <p class="text-sm font-medium text-foreground">Connected</p>
            <p v-if="statusDetail" class="text-sm break-all text-muted-foreground">
              {{ statusDetail }}
            </p>
          </div>
        </div>

        <template v-if="provider === 'openrouter'">
          <div class="space-y-2">
            <Label for="openrouter-api-key">API Key</Label>
            <Input
              id="openrouter-api-key"
              v-model="openRouterApiKey"
              type="password"
              autocomplete="off"
              :placeholder="openRouterApiKeyPlaceholder"
            />
            <p class="text-xs text-muted-foreground">Leave blank to keep the existing key.</p>
          </div>
          <div class="space-y-2">
            <Label for="openrouter-base-url">Base URL</Label>
            <Input
              id="openrouter-base-url"
              v-model="openRouterBaseUrl"
              type="url"
              placeholder="https://openrouter.ai/api/v1"
            />
          </div>
        </template>

        <template v-else-if="provider === 'chatgpt' && !isConnected">
          <template v-if="chatGptOAuthPhase === 'pending'">
            <div class="space-y-3 text-center">
              <p class="text-sm text-muted-foreground">Enter this code at OpenAI:</p>
              <p
                class="font-mono text-3xl font-semibold tracking-widest"
                data-testid="chatgpt-user-code"
              >
                {{ chatGptUserCode }}
              </p>
              <div class="flex justify-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  data-testid="chatgpt-copy-code"
                  @click="copyChatGptUserCode"
                >
                  <Copy class="size-4" />
                  Copy code
                </Button>
                <Button size="sm" variant="outline" as-child>
                  <a
                    :href="chatGptVerificationUrl"
                    target="_blank"
                    rel="noopener noreferrer"
                    data-testid="chatgpt-verification-link"
                  >
                    <ExternalLink class="size-4" />
                    Open authorization page
                  </a>
                </Button>
              </div>
              <p class="flex items-center justify-center gap-2 text-sm text-muted-foreground">
                <Loader2 class="size-4 animate-spin" />
                Waiting for authorization…
              </p>
            </div>
          </template>
          <template v-else>
            <p v-if="chatGptOAuthError" class="text-sm text-destructive">{{ chatGptOAuthError }}</p>
            <Button
              class="w-full"
              :disabled="saving"
              data-testid="chatgpt-connect"
              @click="startChatGptConnect"
            >
              <LoadingSpinner v-if="saving" :size="12" />
              <span v-else>Connect with ChatGPT</span>
            </Button>
          </template>
        </template>

        <template v-else-if="provider === 'openai-compatible'">
          <div class="space-y-2">
            <Label for="compat-base-url">Base URL</Label>
            <Input
              id="compat-base-url"
              v-model="compatBaseUrl"
              type="url"
              autocomplete="off"
              placeholder="https://my-host:8000/v1"
            />
            <p class="text-xs text-muted-foreground">
              OpenAI-compatible endpoint (self-hosted or HuggingFace UI-TARS). Required.
            </p>
          </div>
          <div class="space-y-2">
            <Label for="compat-api-key">API Key (optional)</Label>
            <Input
              id="compat-api-key"
              v-model="compatApiKey"
              type="password"
              autocomplete="off"
              :placeholder="compatApiKeyPlaceholder"
            />
            <p class="text-xs text-muted-foreground">Leave blank to keep the existing key.</p>
          </div>
          <div class="space-y-2">
            <Label for="compat-models">Models (one per line)</Label>
            <Textarea
              id="compat-models"
              v-model="compatModelsText"
              rows="4"
              placeholder="ui-tars-1.5-7b&#10;my-grounding-model"
              data-testid="compat-models"
            />
            <p class="text-xs text-muted-foreground">
              Endpoints without /models are listed by hand. Empty list allowed; entries are trimmed
              and deduplicated on save.
            </p>
          </div>
        </template>

        <template v-else-if="provider === 'amazon-bedrock'">
          <Tabs v-model="bedrockAuthMethod" class="w-full">
            <TabsList class="grid w-full grid-cols-2">
              <TabsTrigger value="access_keys" data-testid="bedrock-tab-access-keys">
                Access keys
              </TabsTrigger>
              <TabsTrigger value="bearer" data-testid="bedrock-tab-bearer">
                Bearer token
              </TabsTrigger>
            </TabsList>
            <TabsContent value="access_keys" class="mt-4 space-y-3">
              <div class="space-y-2">
                <Label for="bedrock-access-key-id">Access key ID</Label>
                <Input
                  id="bedrock-access-key-id"
                  v-model="bedrockAccessKeyId"
                  autocomplete="off"
                  placeholder="AKIA…"
                />
              </div>
              <div class="space-y-2">
                <Label for="bedrock-secret-access-key">Secret access key</Label>
                <Input
                  id="bedrock-secret-access-key"
                  v-model="bedrockSecretAccessKey"
                  type="password"
                  autocomplete="off"
                  placeholder="Leave blank to keep existing"
                />
              </div>
              <div class="space-y-2">
                <Label for="bedrock-session-token">Session token (optional)</Label>
                <Input
                  id="bedrock-session-token"
                  v-model="bedrockSessionToken"
                  type="password"
                  autocomplete="off"
                  placeholder="Leave blank to keep existing"
                />
              </div>
            </TabsContent>
            <TabsContent value="bearer" class="mt-4 space-y-3">
              <div class="space-y-2">
                <Label for="bedrock-bearer-token">Bearer token</Label>
                <Input
                  id="bedrock-bearer-token"
                  v-model="bedrockBearerToken"
                  type="password"
                  autocomplete="off"
                  placeholder="Leave blank to keep existing"
                />
              </div>
            </TabsContent>
          </Tabs>
          <div class="space-y-2">
            <Label for="bedrock-region">Region</Label>
            <Input id="bedrock-region" v-model="bedrockRegion" placeholder="us-east-1" />
          </div>
        </template>

        <div
          v-if="disconnectConfirm"
          class="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
          data-testid="disconnect-confirm"
        >
          <p class="mb-2">{{ meta.disconnectConfirm }}</p>
          <div class="flex gap-2">
            <Button
              size="sm"
              variant="destructive"
              :disabled="saving"
              :data-testid="`confirm-disconnect-${provider}`"
              @click="disconnectProvider"
            >
              Confirm disconnect
            </Button>
            <Button size="sm" variant="outline" @click="disconnectConfirm = false"> Cancel </Button>
          </div>
        </div>
      </DialogBody>

      <DialogFooter v-if="meta" class="gap-2 sm:justify-between">
        <div>
          <Button
            v-if="isConnected && !disconnectConfirm"
            type="button"
            variant="ghost"
            class="text-destructive hover:text-destructive"
            :disabled="saving"
            :data-testid="`disconnect-${provider}`"
            @click="disconnectConfirm = true"
          >
            Disconnect
          </Button>
        </div>
        <div class="flex gap-2">
          <Button type="button" variant="outline" @click="open = false">Close</Button>
          <Button
            v-if="provider === 'openrouter'"
            type="button"
            :disabled="saving"
            data-testid="save-openrouter"
            @click="saveOpenRouter"
          >
            <LoadingSpinner v-if="saving" :size="12" />
            <span v-else>Save</span>
          </Button>
          <Button
            v-else-if="provider === 'openai-compatible'"
            type="button"
            :disabled="saving"
            data-testid="save-compatible"
            @click="saveCompat"
          >
            <LoadingSpinner v-if="saving" :size="12" />
            <span v-else>Save</span>
          </Button>
          <Button
            v-else-if="provider === 'amazon-bedrock'"
            type="button"
            :disabled="saving"
            data-testid="save-bedrock"
            @click="saveBedrock"
          >
            <LoadingSpinner v-if="saving" :size="12" />
            <span v-else>Save</span>
          </Button>
        </div>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
