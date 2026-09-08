<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Bot,
  Cloud,
  Copy,
  ExternalLink,
  Globe,
  Loader2,
} from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
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
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import ProviderModelCombobox from './ProviderModelCombobox.vue'
import SettingsSection from './SettingsSection.vue'
import { type ProviderId, type ProviderModel } from '@/lib/harnessModels'
import { invalidateProviderCatalog, loadProviderModelsCached } from '@/lib/providerCatalog'
import {
  cancelChatGptOAuth,
  deleteProviderConnection,
  getChatGptOAuthStatus,
  getProviderConfig,
  listProviderConnections,
  saveProviderConfig,
  saveProviderConnection,
  startChatGptOAuth,
  type HarnessProviderConfig,
  type ProviderConnection,
} from '@/services/harness.api'

const DEFAULT_OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
const DEFAULT_BEDROCK_REGION = 'us-east-1'

type ProviderCardId = ProviderId

interface ProviderCardMeta {
  id: ProviderCardId
  name: string
  description: string
  icon: typeof Globe
}

const PROVIDER_CARDS: ProviderCardMeta[] = [
  {
    id: 'openrouter',
    name: 'OpenRouter',
    description: 'Route requests through OpenRouter with your API key.',
    icon: Globe,
  },
  {
    id: 'chatgpt',
    name: 'OpenAI ChatGPT',
    description: 'Use your ChatGPT Plus or Pro subscription via device login.',
    icon: Bot,
  },
  {
    id: 'amazon-bedrock',
    name: 'Amazon Bedrock',
    description: 'Connect AWS credentials or a bearer token for Bedrock models.',
    icon: Cloud,
  },
]

const loading = ref(true)
const savingDefaults = ref(false)
const error = ref<string | null>(null)
const config = ref<HarnessProviderConfig | null>(null)
const connections = ref<ProviderConnection[]>([])
const catalog = ref<ProviderModel[]>([])

const defaultModel = ref('')
const smallModel = ref('')
const computerUseModel = ref('')

const activeProvider = ref<ProviderCardId | null>(null)
const dialogOpen = ref(false)
const dialogSaving = ref(false)
const dialogError = ref<string | null>(null)
const disconnectConfirm = ref(false)

const openRouterApiKey = ref('')
const openRouterBaseUrl = ref(DEFAULT_OPENROUTER_BASE_URL)

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

const connectionByProvider = computed(() => {
  const map = new Map<ProviderCardId, ProviderConnection>()
  for (const row of connections.value) {
    map.set(row.provider, row)
  }
  return map
})

const openRouterConnection = computed(() => connectionByProvider.value.get('openrouter'))
const chatGptConnection = computed(() => connectionByProvider.value.get('chatgpt'))
const bedrockConnection = computed(() => connectionByProvider.value.get('amazon-bedrock'))

const openRouterApiKeyPlaceholder = computed(() => {
  const hint = openRouterConnection.value?.api_key_hint
  if (hint) return `Saved key (${hint})`
  return 'sk-or-…'
})

function connectionDetail(provider: ProviderCardId): string {
  const row = connectionByProvider.value.get(provider)
  if (!row?.connected) return ''
  if (provider === 'openrouter') {
    const parts = [row.api_key_hint, row.base_url].filter(Boolean)
    return parts.join(' · ')
  }
  if (provider === 'chatgpt') {
    return row.account_id ? `Account ${row.account_id}` : 'Connected'
  }
  if (provider === 'amazon-bedrock') {
    const auth =
      row.auth_method === 'bearer'
        ? 'Bearer token'
        : row.auth_method === 'access_keys'
          ? 'Access keys'
          : ''
    return [row.region, auth].filter(Boolean).join(' · ')
  }
  return ''
}

function applyConfig(next: HarnessProviderConfig): void {
  config.value = next
  defaultModel.value = next.default_model || ''
  smallModel.value = next.small_model || ''
  computerUseModel.value = next.computer_use_model || ''
}

function resetProviderDialogForms(): void {
  dialogError.value = null
  disconnectConfirm.value = false
  openRouterApiKey.value = ''
  openRouterBaseUrl.value =
    openRouterConnection.value?.base_url || DEFAULT_OPENROUTER_BASE_URL
  bedrockAuthMethod.value =
    bedrockConnection.value?.auth_method === 'bearer' ? 'bearer' : 'access_keys'
  bedrockRegion.value = bedrockConnection.value?.region || DEFAULT_BEDROCK_REGION
  bedrockAccessKeyId.value = ''
  bedrockSecretAccessKey.value = ''
  bedrockSessionToken.value = ''
  bedrockBearerToken.value = ''
  chatGptOAuthError.value = null
  if (chatGptConnection.value?.connected) {
    chatGptOAuthPhase.value = 'connected'
    chatGptAccountId.value = chatGptConnection.value.account_id || ''
  } else {
    chatGptOAuthPhase.value = 'idle'
    chatGptUserCode.value = ''
    chatGptVerificationUrl.value = ''
    chatGptAccountId.value = ''
  }
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
    const message = e instanceof Error ? e.message : 'Failed to load provider settings'
    if (!message.toLowerCase().includes('not found')) {
      error.value = message
    }
  } finally {
    loading.value = false
  }
}

function openProviderDialog(provider: ProviderCardId): void {
  stopChatGptPolling()
  activeProvider.value = provider
  resetProviderDialogForms()
  dialogOpen.value = true
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
      await refreshAll()
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
  dialogSaving.value = true
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
    dialogSaving.value = false
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

async function handleDialogClose(open: boolean): Promise<void> {
  if (open) return
  if (activeProvider.value === 'chatgpt' && chatGptOAuthPhase.value === 'pending') {
    try {
      await cancelChatGptOAuth()
    } catch {
      // Best-effort cancel when the dialog closes mid-flow.
    }
  }
  stopChatGptPolling()
  activeProvider.value = null
  disconnectConfirm.value = false
}

async function saveOpenRouter(): Promise<void> {
  dialogSaving.value = true
  dialogError.value = null
  try {
    await saveProviderConnection('openrouter', {
      api_key: openRouterApiKey.value,
      base_url: openRouterBaseUrl.value.trim() || DEFAULT_OPENROUTER_BASE_URL,
    })
    invalidateProviderCatalog()
    await refreshAll()
    dialogOpen.value = false
  } catch (e: unknown) {
    dialogError.value = e instanceof Error ? e.message : 'Failed to save OpenRouter connection'
  } finally {
    dialogSaving.value = false
  }
}

async function saveBedrock(): Promise<void> {
  dialogSaving.value = true
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
    invalidateProviderCatalog()
    await refreshAll()
    dialogOpen.value = false
  } catch (e: unknown) {
    dialogError.value = e instanceof Error ? e.message : 'Failed to save Bedrock connection'
  } finally {
    dialogSaving.value = false
  }
}

async function disconnectProvider(provider: ProviderCardId): Promise<void> {
  dialogSaving.value = true
  dialogError.value = null
  try {
    await deleteProviderConnection(provider)
    invalidateProviderCatalog()
    await refreshAll()
    dialogOpen.value = false
  } catch (e: unknown) {
    dialogError.value = e instanceof Error ? e.message : 'Failed to disconnect provider'
  } finally {
    dialogSaving.value = false
    disconnectConfirm.value = false
  }
}

async function handleSaveDefaults(): Promise<void> {
  if (savingDefaults.value) return
  savingDefaults.value = true
  error.value = null
  try {
    const saved = await saveProviderConfig({
      default_model: defaultModel.value.trim(),
      small_model: smallModel.value.trim(),
      computer_use_model: computerUseModel.value.trim(),
    })
    applyConfig(saved)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to save default models'
  } finally {
    savingDefaults.value = false
  }
}

watch(dialogOpen, (open) => {
  void handleDialogClose(open)
})

onMounted(() => {
  void loadState()
})

onBeforeUnmount(() => {
  stopChatGptPolling()
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
        <div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <Card
            v-for="card in PROVIDER_CARDS"
            :key="card.id"
            class="flex flex-col"
            :data-testid="`provider-card-${card.id}`"
          >
            <CardHeader class="space-y-3">
              <div class="flex items-start justify-between gap-2">
                <div class="flex items-center gap-2">
                  <component :is="card.icon" class="size-5 text-muted-foreground" />
                  <CardTitle class="text-base">{{ card.name }}</CardTitle>
                </div>
                <Badge
                  :variant="connectionByProvider.get(card.id)?.connected ? 'default' : 'outline'"
                >
                  {{ connectionByProvider.get(card.id)?.connected ? 'Connected' : 'Not connected' }}
                </Badge>
              </div>
              <CardDescription>{{ card.description }}</CardDescription>
            </CardHeader>
            <CardContent class="mt-auto space-y-3">
              <p
                v-if="connectionByProvider.get(card.id)?.connected"
                class="text-xs text-muted-foreground"
                :data-testid="`provider-detail-${card.id}`"
              >
                {{ connectionDetail(card.id) }}
              </p>
              <Button
                size="sm"
                variant="outline"
                class="w-full"
                :data-testid="`provider-manage-${card.id}`"
                @click="openProviderDialog(card.id)"
              >
                {{ connectionByProvider.get(card.id)?.connected ? 'Manage' : 'Connect' }}
              </Button>
            </CardContent>
          </Card>
        </div>
      </SettingsSection>

      <SettingsSection
        title="Default Models"
        description="Org-wide defaults for new sessions. Model ids use provider/model format from any connected provider."
      >
        <div class="grid max-w-xl gap-4">
          <div class="space-y-2">
            <Label for="provider-default-model">Default Model</Label>
            <ProviderModelCombobox
              input-id="provider-default-model"
              v-model="defaultModel"
              :models="catalog"
              empty-hint="Connect a provider to browse models, or enter a provider/model id manually."
            />
          </div>
          <div class="space-y-2">
            <Label for="provider-small-model">Small Model</Label>
            <ProviderModelCombobox
              input-id="provider-small-model"
              v-model="smallModel"
              :models="catalog"
              empty-hint="Connect a provider to browse models, or enter a provider/model id manually."
            />
          </div>
          <div class="space-y-2">
            <Label for="provider-computer-use-model">Computer-use model</Label>
            <ProviderModelCombobox
              input-id="provider-computer-use-model"
              v-model="computerUseModel"
              :models="catalog"
              empty-hint="Connect a provider to browse models, or enter a provider/model id manually."
            />
          </div>
        </div>
        <div class="flex justify-end">
          <Button
            size="sm"
            :disabled="savingDefaults"
            data-testid="save-default-models"
            @click="handleSaveDefaults"
          >
            <LoadingSpinner v-if="savingDefaults" :size="12" />
            <span v-else>Save Default Models</span>
          </Button>
        </div>
      </SettingsSection>
    </template>

    <Dialog v-model:open="dialogOpen">
      <DialogContent class="sm:max-w-md">
        <DialogHeader v-if="activeProvider === 'openrouter'">
          <DialogTitle>OpenRouter</DialogTitle>
          <DialogDescription>
            Enter your OpenRouter API key and optional custom base URL.
          </DialogDescription>
        </DialogHeader>
        <DialogHeader v-else-if="activeProvider === 'chatgpt'">
          <DialogTitle>OpenAI ChatGPT</DialogTitle>
          <DialogDescription>
            Use your ChatGPT Plus or Pro subscription. You will authorize via OpenAI device login.
          </DialogDescription>
        </DialogHeader>
        <DialogHeader v-else-if="activeProvider === 'amazon-bedrock'">
          <DialogTitle>Amazon Bedrock</DialogTitle>
          <DialogDescription>
            Connect with IAM access keys or a bearer token for your Bedrock region.
          </DialogDescription>
        </DialogHeader>

        <DialogBody class="space-y-4">
          <p v-if="dialogError" class="text-sm text-destructive">{{ dialogError }}</p>

          <template v-if="activeProvider === 'openrouter'">
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
            <div
              v-if="openRouterConnection?.connected && disconnectConfirm"
              class="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
            >
              <p class="mb-2">Disconnect OpenRouter? The harness will stop using this API key.</p>
              <div class="flex gap-2">
                <Button
                  size="sm"
                  variant="destructive"
                  :disabled="dialogSaving"
                  data-testid="confirm-disconnect-openrouter"
                  @click="disconnectProvider('openrouter')"
                >
                  Confirm disconnect
                </Button>
                <Button size="sm" variant="outline" @click="disconnectConfirm = false">
                  Cancel
                </Button>
              </div>
            </div>
          </template>

          <template v-else-if="activeProvider === 'chatgpt'">
            <template v-if="chatGptConnection?.connected || chatGptOAuthPhase === 'connected'">
              <div class="rounded-md border border-border bg-muted/40 p-4 text-sm">
                <p class="font-medium text-foreground">Connected</p>
                <p v-if="chatGptAccountId || chatGptConnection?.account_id" class="text-muted-foreground">
                  Account {{ chatGptAccountId || chatGptConnection?.account_id }}
                </p>
              </div>
              <div
                v-if="disconnectConfirm"
                class="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
              >
                <p class="mb-2">Disconnect ChatGPT? Subscription access will be removed.</p>
                <div class="flex gap-2">
                  <Button
                    size="sm"
                    variant="destructive"
                    :disabled="dialogSaving"
                    data-testid="confirm-disconnect-chatgpt"
                    @click="disconnectProvider('chatgpt')"
                  >
                    Confirm disconnect
                  </Button>
                  <Button size="sm" variant="outline" @click="disconnectConfirm = false">
                    Cancel
                  </Button>
                </div>
              </div>
            </template>
            <template v-else-if="chatGptOAuthPhase === 'pending'">
              <div class="space-y-3 text-center">
                <p class="text-sm text-muted-foreground">Enter this code at OpenAI:</p>
                <p
                  class="font-mono text-3xl font-semibold tracking-widest"
                  data-testid="chatgpt-user-code"
                >
                  {{ chatGptUserCode }}
                </p>
                <div class="flex justify-center gap-2">
                  <Button size="sm" variant="outline" data-testid="chatgpt-copy-code" @click="copyChatGptUserCode">
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
                :disabled="dialogSaving"
                data-testid="chatgpt-connect"
                @click="startChatGptConnect"
              >
                <LoadingSpinner v-if="dialogSaving" :size="12" />
                <span v-else>Connect with ChatGPT</span>
              </Button>
            </template>
          </template>

          <template v-else-if="activeProvider === 'amazon-bedrock'">
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
              <Input
                id="bedrock-region"
                v-model="bedrockRegion"
                placeholder="us-east-1"
              />
            </div>
            <div
              v-if="bedrockConnection?.connected && disconnectConfirm"
              class="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
            >
              <p class="mb-2">Disconnect Amazon Bedrock?</p>
              <div class="flex gap-2">
                <Button
                  size="sm"
                  variant="destructive"
                  :disabled="dialogSaving"
                  data-testid="confirm-disconnect-bedrock"
                  @click="disconnectProvider('amazon-bedrock')"
                >
                  Confirm disconnect
                </Button>
                <Button size="sm" variant="outline" @click="disconnectConfirm = false">
                  Cancel
                </Button>
              </div>
            </div>
          </template>
        </DialogBody>

        <DialogFooter class="gap-2 sm:justify-between">
          <div>
            <Button
              v-if="
                activeProvider &&
                ((activeProvider === 'openrouter' && openRouterConnection?.connected) ||
                  (activeProvider === 'chatgpt' &&
                    (chatGptConnection?.connected || chatGptOAuthPhase === 'connected')) ||
                  (activeProvider === 'amazon-bedrock' && bedrockConnection?.connected)) &&
                !disconnectConfirm
              "
              type="button"
              variant="ghost"
              class="text-destructive hover:text-destructive"
              :disabled="dialogSaving"
              :data-testid="`disconnect-${activeProvider}`"
              @click="disconnectConfirm = true"
            >
              Disconnect
            </Button>
          </div>
          <div class="flex gap-2">
            <Button type="button" variant="outline" @click="dialogOpen = false">Close</Button>
            <Button
              v-if="activeProvider === 'openrouter'"
              type="button"
              :disabled="dialogSaving"
              data-testid="save-openrouter"
              @click="saveOpenRouter"
            >
              <LoadingSpinner v-if="dialogSaving" :size="12" />
              <span v-else>Save</span>
            </Button>
            <Button
              v-else-if="activeProvider === 'amazon-bedrock'"
              type="button"
              :disabled="dialogSaving"
              data-testid="save-bedrock"
              @click="saveBedrock"
            >
              <LoadingSpinner v-if="dialogSaving" :size="12" />
              <span v-else>Save</span>
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
