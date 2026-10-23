<!-- Account picker and provider-specific account editor. -->
<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ExternalLink, Loader2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
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
import CopyButton from '@/components/common/CopyButton.vue'
import { providerMeta, type SettingsProviderId } from './providerMeta'
import {
  cancelChatGptOAuth,
  createProviderAccount,
  deleteProviderAccount,
  getChatGptOAuthStatus,
  notifyProviderAccountsChanged,
  selectProviderAccount,
  startChatGptOAuth,
  updateProviderAccount,
  type ProviderAccount,
  type ProviderAccountPayload,
} from '@/services/harness.api'

import { useAutosave } from '@/composables/useAutosave'
import AutosaveStatus from './AutosaveStatus.vue'

const DEFAULT_OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
const DEFAULT_BEDROCK_REGION = 'us-east-1'

const props = withDefaults(
  defineProps<{
    provider: SettingsProviderId | null
    accounts?: ProviderAccount[]
    isAdmin?: boolean
  }>(),
  { accounts: () => [], isAdmin: false },
)
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ changed: [] }>()

const meta = computed(() => providerMeta(props.provider))
const selectedAccount = ref<ProviderAccount | null>(null)
const creating = ref(false)
const saving = ref(false)
const savedAccountForm = ref('')
const dialogError = ref<string | null>(null)
const confirmAction = ref<'delete' | 'unshare' | null>(null)
const label = ref('')
const shared = ref(false)
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
const claudeAuthType = ref<'api_token' | 'subscription_token'>('api_token')
const claudeToken = ref('')
type OAuthPhase = 'idle' | 'pending' | 'connected' | 'expired' | 'denied' | 'error'
const oauthPhase = ref<OAuthPhase>('idle')
const oauthFlowId = ref<string | null>(null)
const oauthUserCode = ref('')
const oauthVerificationUrl = ref('')
const oauthAccountId = ref('')
const oauthError = ref<string | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollIntervalSec = 5
let dialogGeneration = 0

const isEditing = computed(() => creating.value || selectedAccount.value !== null)
const canEdit = computed(() => creating.value || Boolean(selectedAccount.value?.can_manage))
const secretHint = computed(() => selectedAccount.value?.api_key_hint || '')

function resetForm(account: ProviderAccount | null = null, isCreating = false): void {
  stopPolling()
  void cancelOAuth()
  dialogGeneration += 1
  selectedAccount.value = account
  creating.value = isCreating
  dialogError.value = null
  confirmAction.value = null
  label.value = account?.label ?? ''
  shared.value = account?.scope === 'organization'
  openRouterApiKey.value = ''
  openRouterBaseUrl.value = account?.base_url || DEFAULT_OPENROUTER_BASE_URL
  compatApiKey.value = ''
  compatBaseUrl.value = account?.base_url || ''
  compatModelsText.value = (account?.models ?? []).join('\n')
  bedrockAuthMethod.value = account?.auth_method === 'bearer' ? 'bearer' : 'access_keys'
  bedrockRegion.value = account?.region || DEFAULT_BEDROCK_REGION
  bedrockAccessKeyId.value = ''
  bedrockSecretAccessKey.value = ''
  bedrockSessionToken.value = ''
  bedrockBearerToken.value = ''
  claudeAuthType.value =
    account?.auth_type === 'subscription_token' ? 'subscription_token' : 'api_token'
  claudeToken.value = ''
  oauthPhase.value = 'idle'
  oauthFlowId.value = null
  oauthUserCode.value = ''
  oauthVerificationUrl.value = ''
  oauthAccountId.value = ''
  oauthError.value = null
  savedAccountForm.value = JSON.stringify(accountFormSnapshot())
}

function openCreateForm(): void {
  resetForm(null, true)
}

function openEditForm(account: ProviderAccount): void {
  resetForm(account)
}

function parseModels(text: string): string[] {
  return [
    ...new Set(
      text
        .split('\n')
        .map((value) => value.trim())
        .filter(Boolean),
    ),
  ]
}

function payloadForAccount(reportErrors = true): ProviderAccountPayload | null {
  if (!props.provider) return null
  const base: ProviderAccountPayload = {
    label: label.value.trim(),
    shared: props.isAdmin && shared.value,
  }
  if (!base.label) {
    if (reportErrors) dialogError.value = 'Account name is required.'
    return null
  }
  switch (props.provider) {
    case 'openrouter':
      return {
        ...base,
        api_key: openRouterApiKey.value,
        base_url: openRouterBaseUrl.value.trim() || DEFAULT_OPENROUTER_BASE_URL,
      }
    case 'openai-compatible':
      if (!compatBaseUrl.value.trim()) {
        if (reportErrors) dialogError.value = 'Base URL is required.'
        return null
      }
      return {
        ...base,
        api_key: compatApiKey.value,
        base_url: compatBaseUrl.value.trim(),
        models: parseModels(compatModelsText.value),
      }
    case 'amazon-bedrock':
      return bedrockAuthMethod.value === 'bearer'
        ? {
            ...base,
            auth_method: 'bearer',
            region: bedrockRegion.value.trim() || DEFAULT_BEDROCK_REGION,
            bearer_token: bedrockBearerToken.value,
          }
        : {
            ...base,
            auth_method: 'access_keys',
            region: bedrockRegion.value.trim() || DEFAULT_BEDROCK_REGION,
            access_key_id: bedrockAccessKeyId.value,
            secret_access_key: bedrockSecretAccessKey.value,
            session_token: bedrockSessionToken.value,
          }
    case 'claude-agent': {
      const token = claudeToken.value.trim()
      if (!selectedAccount.value?.connected && !token) {
        if (reportErrors) dialogError.value = 'Enter an Anthropic token to connect this account.'
        return null
      }
      if (
        selectedAccount.value?.connected &&
        selectedAccount.value.auth_type !== claudeAuthType.value &&
        !token
      ) {
        if (reportErrors) dialogError.value = 'Enter a token when changing the authentication type.'
        return null
      }
      return { ...base, auth_type: claudeAuthType.value, token }
    }
    case 'chatgpt':
      return base
  }
}

function accountFormSnapshot() {
  return [
    label.value,
    shared.value,
    openRouterApiKey.value,
    openRouterBaseUrl.value,
    compatApiKey.value,
    compatBaseUrl.value,
    compatModelsText.value,
    bedrockAuthMethod.value,
    bedrockRegion.value,
    bedrockAccessKeyId.value,
    bedrockSecretAccessKey.value,
    bedrockSessionToken.value,
    bedrockBearerToken.value,
    claudeAuthType.value,
    claudeToken.value,
  ]
}

const accountAutosave = useAutosave({
  source: accountFormSnapshot,
  dirty: () => JSON.stringify(accountFormSnapshot()) !== savedAccountForm.value,
  enabled: () => open.value && !!selectedAccount.value && canEdit.value,
  valid: () =>
    !!payloadForAccount(false) &&
    !confirmAction.value &&
    !saving.value &&
    !(selectedAccount.value?.scope === 'organization' && !shared.value) &&
    (props.provider !== 'amazon-bedrock' ||
      (bedrockAuthMethod.value === 'bearer'
        ? !!bedrockBearerToken.value || selectedAccount.value?.auth_method === 'bearer'
        : ((!bedrockAccessKeyId.value && !bedrockSecretAccessKey.value) ||
            (!!bedrockAccessKeyId.value && !!bedrockSecretAccessKey.value)) &&
          (selectedAccount.value?.auth_method !== 'bearer' || !!bedrockAccessKeyId.value))),
  scope: () => [props.provider, selectedAccount.value?.id],
  errorMessage: 'Failed to update provider account',
  save: async (isCurrent) => {
    const snapshot = JSON.stringify(accountFormSnapshot())
    const payload = payloadForAccount(false)!
    const updated = await updateProviderAccount(props.provider!, selectedAccount.value!.id, payload)
    notifyProviderAccountsChanged()
    if (!isCurrent()) return
    savedAccountForm.value = snapshot
    selectedAccount.value = updated
    emit('changed')
  },
})

async function backToAccounts(): Promise<void> {
  if (await accountAutosave.flush()) resetForm()
}

watch(shared, (value) => {
  if (!value && selectedAccount.value?.scope === 'organization' && props.isAdmin)
    confirmAction.value = 'unshare'
})

function cancelAction(): void {
  shared.value = selectedAccount.value?.scope === 'organization'
  confirmAction.value = null
}

async function saveAccount(): Promise<void> {
  if (!props.provider || !canEdit.value) return
  dialogError.value = null
  if (selectedAccount.value?.scope === 'organization' && props.isAdmin && !shared.value) {
    confirmAction.value = 'unshare'
    return
  }
  const payload = payloadForAccount()
  if (!payload) return
  saving.value = true
  try {
    if (selectedAccount.value) {
      await updateProviderAccount(props.provider, selectedAccount.value.id, payload)
    } else if (props.provider !== 'chatgpt') {
      await createProviderAccount(props.provider, payload)
    } else {
      await startChatGptAuthorization()
      return
    }
    resetForm()
    notifyProviderAccountsChanged()
    emit('changed')
  } catch (error: unknown) {
    dialogError.value = error instanceof Error ? error.message : 'Failed to save account'
  } finally {
    saving.value = false
  }
}

async function selectAccount(accountId: string | null): Promise<void> {
  if (!props.provider) return
  saving.value = true
  dialogError.value = null
  try {
    await selectProviderAccount(props.provider, accountId)
    notifyProviderAccountsChanged()
    emit('changed')
  } catch (error: unknown) {
    dialogError.value = error instanceof Error ? error.message : 'Failed to change active account'
  } finally {
    saving.value = false
  }
}

async function deleteAccount(): Promise<void> {
  if (!props.provider || !selectedAccount.value?.can_manage) return
  saving.value = true
  dialogError.value = null
  try {
    await deleteProviderAccount(props.provider, selectedAccount.value.id)
    notifyProviderAccountsChanged()
    emit('changed')
    resetForm()
  } catch (error: unknown) {
    dialogError.value = error instanceof Error ? error.message : 'Failed to delete account'
  } finally {
    saving.value = false
    confirmAction.value = null
  }
}

function askToUnshare(): void {
  if (!props.isAdmin || !selectedAccount.value?.can_manage) return
  confirmAction.value = 'unshare'
}

async function confirmUnshare(): Promise<void> {
  const account = selectedAccount.value
  if (!props.provider || !account) return
  saving.value = true
  dialogError.value = null
  try {
    const payload = payloadForAccount()
    if (!payload) return
    if (props.provider === 'openrouter') payload.api_key = ''
    if (props.provider === 'openai-compatible') payload.api_key = ''
    if (props.provider === 'amazon-bedrock') {
      delete payload.access_key_id
      delete payload.secret_access_key
      delete payload.session_token
      delete payload.bearer_token
    }
    if (props.provider === 'claude-agent') payload.token = ''
    payload.shared = false
    await updateProviderAccount(props.provider, account.id, payload)
    notifyProviderAccountsChanged()
    emit('changed')
    resetForm()
  } catch (error: unknown) {
    dialogError.value = error instanceof Error ? error.message : 'Failed to unshare account'
  } finally {
    saving.value = false
    confirmAction.value = null
  }
}

function stopPolling(): void {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
}

async function pollOAuth(): Promise<void> {
  const flowId = oauthFlowId.value
  if (!flowId) return
  const generation = dialogGeneration
  try {
    const status = await getChatGptOAuthStatus(flowId)
    if (generation !== dialogGeneration || flowId !== oauthFlowId.value) return
    if (status.status === 'pending') {
      stopPolling()
      pollTimer = setTimeout(() => void pollOAuth(), pollIntervalSec * 1000)
      return
    }
    stopPolling()
    if (status.status === 'connected') {
      oauthPhase.value = 'connected'
      oauthAccountId.value = status.account_id || ''
      notifyProviderAccountsChanged()
      emit('changed')
      resetForm()
      return
    }
    oauthPhase.value =
      status.status === 'denied' ? 'denied' : status.status === 'cancelled' ? 'idle' : 'expired'
    oauthError.value =
      status.status === 'denied'
        ? 'Authorization was denied. Try again when ready.'
        : status.status === 'cancelled'
          ? null
          : 'The authorization code expired. Start a new connection.'
  } catch (error: unknown) {
    if (generation !== dialogGeneration || flowId !== oauthFlowId.value) return
    stopPolling()
    oauthPhase.value = 'error'
    oauthError.value = error instanceof Error ? error.message : 'OAuth polling failed'
  }
}

async function startChatGptAuthorization(): Promise<void> {
  if (!props.provider || props.provider !== 'chatgpt') return
  oauthError.value = null
  const generation = dialogGeneration
  await cancelOAuth()
  stopPolling()
  saving.value = true
  try {
    const result = await startChatGptOAuth({
      label: label.value.trim(),
      shared: props.isAdmin && shared.value,
      ...(selectedAccount.value ? { account_id: selectedAccount.value.id } : {}),
    })
    if (!result.flow_id) throw new Error('OAuth flow could not be scoped to this account.')
    if (generation !== dialogGeneration || !open.value) {
      await cancelChatGptOAuth(result.flow_id).catch(() => undefined)
      return
    }
    oauthFlowId.value = result.flow_id
    oauthUserCode.value = result.user_code
    oauthVerificationUrl.value = result.verification_url
    pollIntervalSec = Math.max(1, result.interval)
    oauthPhase.value = 'pending'
    stopPolling()
    pollTimer = setTimeout(() => void pollOAuth(), pollIntervalSec * 1000)
  } catch (error: unknown) {
    oauthPhase.value = 'error'
    oauthError.value = error instanceof Error ? error.message : 'Failed to start OAuth'
  } finally {
    saving.value = false
  }
}

async function cancelOAuth(): Promise<void> {
  const flowId = oauthFlowId.value
  oauthFlowId.value = null
  if (flowId) await cancelChatGptOAuth(flowId).catch(() => undefined)
}

async function closeDialog(nextOpen: boolean): Promise<void> {
  if (!nextOpen && !(await accountAutosave.flush())) return
  open.value = nextOpen
  if (!nextOpen) {
    stopPolling()
    await cancelOAuth()
    resetForm()
  }
}

watch(
  () => [open.value, props.provider] as const,
  ([isOpen], previous) => {
    if (isOpen && !previous?.[0]) resetForm()
    if (!isOpen && previous?.[0]) {
      stopPolling()
      void cancelOAuth()
      resetForm()
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  stopPolling()
  void cancelOAuth()
})
</script>

<template>
  <Dialog :open="open" @update:open="closeDialog">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader v-if="meta">
        <DialogTitle>{{ meta.name }} accounts</DialogTitle>
        <DialogDescription
          >Choose which account new responses use, or manage an account.</DialogDescription
        >
      </DialogHeader>

      <DialogBody v-if="meta" class="max-h-[70vh] space-y-4 overflow-y-auto">
        <p v-if="dialogError" class="text-sm text-destructive">{{ dialogError }}</p>

        <template v-if="!isEditing">
          <div
            class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
          >
            <div
              v-for="account in accounts"
              :key="account.id"
              class="flex items-center gap-3 px-3 py-3"
            >
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm font-medium">{{ account.label }}</p>
                <p class="text-xs text-muted-foreground">
                  {{ account.scope === 'organization' ? 'Organization shared' : 'Personal' }}
                  <span v-if="!account.connected"> · Not connected</span>
                </p>
              </div>
              <span v-if="account.is_active" class="text-xs font-medium text-success">Active</span>
              <Button
                v-else
                size="sm"
                variant="outline"
                :disabled="saving || !account.connected"
                @click="selectAccount(account.id)"
                >Use</Button
              >
              <Button
                v-if="account.can_manage"
                size="sm"
                variant="ghost"
                :data-testid="`edit-account-${account.id}`"
                @click="openEditForm(account)"
                >Edit</Button
              >
            </div>
            <div
              v-if="!accounts.length"
              class="px-3 py-5 text-center text-sm text-muted-foreground"
            >
              No accounts yet.
            </div>
          </div>
          <Button
            v-if="accounts.length"
            size="sm"
            variant="ghost"
            class="w-full"
            :disabled="saving || !accounts.some((account) => account.is_active)"
            data-testid="select-no-account"
            @click="selectAccount(null)"
            >Use no account</Button
          >
          <Button
            class="w-full"
            :disabled="saving"
            data-testid="add-provider-account"
            @click="openCreateForm"
            >Add account</Button
          >
        </template>

        <template v-else>
          <Button size="sm" variant="ghost" class="-ml-2" @click="backToAccounts">← Accounts</Button>
          <div class="space-y-2">
            <Label for="account-label">Account name</Label>
            <Input
              id="account-label"
              v-model="label"
              autocomplete="off"
              placeholder="e.g. Personal or Production"
            />
          </div>
          <label v-if="isAdmin" class="flex items-center gap-2 text-sm">
            <Checkbox
              v-model="shared"
              :disabled="Boolean(selectedAccount && !selectedAccount.can_manage)"
              data-testid="account-shared"
            />
            Share with this organization
          </label>
          <p
            v-if="!isAdmin && selectedAccount?.scope === 'personal'"
            class="text-xs text-muted-foreground"
          >
            Only you can use this account.
          </p>

          <template v-if="provider === 'openrouter'">
            <div class="space-y-2">
              <Label for="openrouter-api-key">API key</Label
              ><Input
                id="openrouter-api-key"
                v-model="openRouterApiKey"
                type="password"
                autocomplete="off"
                :placeholder="secretHint ? `Saved key (${secretHint})` : 'sk-or-…'"
              />
              <p class="text-xs text-muted-foreground">Leave blank to keep the saved key.</p>
            </div>
            <div class="space-y-2">
              <Label for="openrouter-base-url">Base URL</Label
              ><Input
                id="openrouter-base-url"
                v-model="openRouterBaseUrl"
                type="url"
                placeholder="https://openrouter.ai/api/v1"
              />
            </div>
          </template>

          <template v-else-if="provider === 'chatgpt'">
            <template v-if="oauthPhase === 'pending'">
              <div class="space-y-3 text-center">
                <p class="text-sm text-muted-foreground">Enter this code at OpenAI:</p>
                <p
                  class="font-mono text-3xl font-semibold tracking-widest"
                  data-testid="chatgpt-user-code"
                >
                  {{ oauthUserCode }}
                </p>
                <div class="flex justify-center gap-2">
                  <CopyButton :text="oauthUserCode" label="login code" size="sm" variant="outline"
                    >Copy code</CopyButton
                  ><Button size="sm" variant="outline" as-child
                    ><a :href="oauthVerificationUrl" target="_blank" rel="noopener noreferrer"
                      ><ExternalLink class="size-4" /> Open authorization page</a
                    ></Button
                  >
                </div>
                <p class="flex items-center justify-center gap-2 text-sm text-muted-foreground">
                  <Loader2 class="size-4 animate-spin" />Waiting for authorization…
                </p>
              </div>
            </template>
            <template v-else>
              <p v-if="oauthError" class="text-sm text-destructive">{{ oauthError }}</p>
              <Button
                class="w-full"
                :disabled="saving || !label.trim()"
                data-testid="chatgpt-connect"
                @click="startChatGptAuthorization"
                ><LoadingSpinner v-if="saving" :size="12" /><span v-else>{{
                  selectedAccount ? 'Reconnect with ChatGPT' : 'Connect with ChatGPT'
                }}</span></Button
              >
            </template>
          </template>

          <template v-else-if="provider === 'openai-compatible'">
            <div class="space-y-2">
              <Label for="compat-base-url">Base URL</Label
              ><Input
                id="compat-base-url"
                v-model="compatBaseUrl"
                type="url"
                autocomplete="off"
                placeholder="https://my-host:8000/v1"
              />
            </div>
            <div class="space-y-2">
              <Label for="compat-api-key">API key (optional)</Label
              ><Input
                id="compat-api-key"
                v-model="compatApiKey"
                type="password"
                autocomplete="off"
                :placeholder="secretHint ? `Saved key (${secretHint})` : 'Optional API key'"
              />
              <p class="text-xs text-muted-foreground">Leave blank to keep the saved key.</p>
            </div>
            <div class="space-y-2">
              <Label for="compat-models">Models (one per line)</Label
              ><Textarea
                id="compat-models"
                v-model="compatModelsText"
                rows="4"
                placeholder="ui-tars-1.5-7b&#10;my-grounding-model"
              />
            </div>
          </template>

          <template v-else-if="provider === 'claude-agent'">
            <div class="space-y-2">
              <Label for="claude-auth-type">Anthropic authentication</Label
              ><Tabs v-model="claudeAuthType"
                ><TabsList class="grid w-full grid-cols-2"
                  ><TabsTrigger value="api_token">API token</TabsTrigger
                  ><TabsTrigger value="subscription_token"
                    >Subscription token</TabsTrigger
                  ></TabsList
                ></Tabs
              >
            </div>
            <div class="space-y-2">
              <Label for="claude-token">{{
                claudeAuthType === 'api_token'
                  ? 'Anthropic API token'
                  : 'Anthropic subscription token'
              }}</Label
              ><Input
                id="claude-token"
                v-model="claudeToken"
                type="password"
                autocomplete="off"
                :placeholder="
                  selectedAccount?.connected && selectedAccount.auth_type === claudeAuthType
                    ? 'Leave blank to keep existing token'
                    : 'Paste your Anthropic token'
                "
              />
              <p class="text-xs text-muted-foreground">Token usage follows Anthropic's terms.</p>
            </div>
          </template>

          <template v-else-if="provider === 'amazon-bedrock'">
            <Tabs v-model="bedrockAuthMethod"
              ><TabsList class="grid w-full grid-cols-2"
                ><TabsTrigger value="access_keys">Access keys</TabsTrigger
                ><TabsTrigger value="bearer">Bearer token</TabsTrigger></TabsList
              ><TabsContent value="access_keys" class="mt-4 space-y-3"
                ><div class="space-y-2">
                  <Label for="bedrock-access-key-id">Access key ID</Label
                  ><Input
                    id="bedrock-access-key-id"
                    v-model="bedrockAccessKeyId"
                    autocomplete="off"
                    placeholder="AKIA…"
                  />
                </div>
                <div class="space-y-2">
                  <Label for="bedrock-secret-access-key">Secret access key</Label
                  ><Input
                    id="bedrock-secret-access-key"
                    v-model="bedrockSecretAccessKey"
                    type="password"
                    autocomplete="off"
                    placeholder="Leave blank to keep existing"
                  />
                </div>
                <div class="space-y-2">
                  <Label for="bedrock-session-token">Session token (optional)</Label
                  ><Input
                    id="bedrock-session-token"
                    v-model="bedrockSessionToken"
                    type="password"
                    autocomplete="off"
                    placeholder="Leave blank to keep existing"
                  /></div></TabsContent
              ><TabsContent value="bearer" class="mt-4 space-y-3"
                ><div class="space-y-2">
                  <Label for="bedrock-bearer-token">Bearer token</Label
                  ><Input
                    id="bedrock-bearer-token"
                    v-model="bedrockBearerToken"
                    type="password"
                    autocomplete="off"
                    placeholder="Leave blank to keep existing"
                  /></div></TabsContent
            ></Tabs>
            <div class="space-y-2">
              <Label for="bedrock-region">Region</Label
              ><Input id="bedrock-region" v-model="bedrockRegion" placeholder="us-east-1" />
            </div>
          </template>

          <div
            v-if="confirmAction"
            class="space-y-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm"
            data-testid="account-confirm"
          >
            <p v-if="confirmAction === 'delete'">
              Deleting this account immediately interrupts responses using it. They will not switch
              to another account.
            </p>
            <p v-else>
              Unsharing transfers this account to you and immediately interrupts other members'
              responses using it. They will not switch to another account.
            </p>
            <div class="flex gap-2">
              <Button
                size="sm"
                variant="destructive"
                data-testid="confirm-account-action"
                :disabled="saving || accountAutosave.saving"
                @click="confirmAction === 'delete' ? deleteAccount() : confirmUnshare()"
                >Confirm</Button
              ><Button size="sm" variant="outline" @click="cancelAction">Cancel</Button>
            </div>
          </div>
        </template>
      </DialogBody>

      <DialogFooter v-if="meta" class="gap-2 sm:justify-between">
        <div class="flex gap-2">
          <Button
            v-if="isEditing && selectedAccount?.can_manage && !confirmAction"
            type="button"
            variant="ghost"
            class="text-destructive hover:text-destructive"
            :disabled="saving || accountAutosave.saving"
            data-testid="delete-account"
            @click="confirmAction = 'delete'"
            >Delete</Button
          >
          <Button
            v-if="
              isEditing &&
              selectedAccount?.scope === 'organization' &&
              selectedAccount.can_manage &&
              isAdmin &&
              !confirmAction
            "
            type="button"
            variant="ghost"
            :disabled="saving || accountAutosave.saving"
            data-testid="unshare-account"
            @click="askToUnshare"
            >Unshare</Button
          >
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <AutosaveStatus v-if="selectedAccount && canEdit" :state="accountAutosave" />
          <Button
            type="button"
            variant="outline"
            @click="isEditing ? backToAccounts() : closeDialog(false)"
            >{{ isEditing ? 'Back' : 'Close' }}</Button
          >
          <Button
            v-if="creating && canEdit && provider !== 'chatgpt'"
            type="button"
            :disabled="saving"
            data-testid="save-provider-account"
            @click="saveAccount"
            ><LoadingSpinner v-if="saving" :size="12" /><span v-else>Add account</span></Button
          >
        </div>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
