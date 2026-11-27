<!--
  PluginVariableRow — one environment variable or header of an MCP server.
  A secret's value never stays in the plugin: it is stored as a credential
  the plugin uses, taken from an existing credential, or attached by each
  workspace itself.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { Check, ChevronDown, KeyRound, Lock, LockOpen, Users, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import {
  isSavedSecret,
  looksSecret,
  type SecretSource,
  type VariableRow,
} from '@opencuria/chat-core/pluginForms'

export interface CredentialOption {
  id: string
  name: string
  service_id: string
  service_name: string
  credential_type?: string
}

const props = defineProps<{
  label: string
  index: number
  credentials: CredentialOption[]
  disabled?: boolean
}>()
const row = defineModel<VariableRow>({ required: true })
const emit = defineEmits<{ remove: [] }>()

const saved = computed(() => isSavedSecret(row.value))
/** Other credentials to use; the saved one is "Keep or replace the saved value". */
const choices = computed(() =>
  props.credentials.filter(
    (credential) => !saved.value || credential.id !== row.value.credentialId,
  ),
)
const name = computed(
  () => row.value.key.trim() || `${props.label.toLowerCase()} ${props.index + 1}`,
)

function update(patch: Partial<VariableRow>): void {
  const next = { ...row.value, ...patch }
  // Suggest secrecy once for names like API_KEY; the user can undo it.
  if (patch.key !== undefined && !row.value.key && !row.value.secret && looksSecret(next.key))
    next.secret = true
  row.value = next
}

function toggleSecret(): void {
  if (row.value.secret)
    update({
      secret: false,
      value: '',
      source: 'value',
      credentialId: '',
      credentialName: '',
      serviceId: '',
      requirementKey: '',
    })
  else update({ secret: true, source: 'value' })
}

function setSource(source: SecretSource): void {
  if (source === row.value.source) return
  update({ source, value: '', credentialId: '', credentialName: '' })
}

function chooseCredential(credential: CredentialOption): void {
  update({
    source: 'credential',
    value: '',
    credentialId: credential.id,
    credentialName: credential.name,
    serviceId: credential.service_id,
  })
}
</script>

<template>
  <div class="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto_auto] items-center gap-1.5">
    <Input
      :model-value="row.key"
      class="font-mono text-xs"
      spellcheck="false"
      :placeholder="label === 'Header' ? 'Header' : 'NAME'"
      :aria-label="`${label} ${index + 1} name`"
      :disabled="disabled"
      @update:model-value="update({ key: String($event) })"
    />
    <div class="flex min-w-0 items-center gap-1">
      <Input
        v-if="!row.secret"
        :model-value="row.value"
        class="font-mono text-xs"
        spellcheck="false"
        :aria-label="`${label} ${index + 1} value`"
        :disabled="disabled"
        @update:model-value="update({ value: String($event) })"
      />
      <Input
        v-else-if="row.source === 'value'"
        :model-value="row.value"
        type="password"
        class="text-xs"
        autocomplete="new-password"
        :placeholder="saved ? `Saved · ${row.credentialName}` : 'Value'"
        :title="
          saved ? `Saved as the credential “${row.credentialName}”. Type to replace it.` : undefined
        "
        :aria-label="`${label} ${index + 1} secret value`"
        :disabled="disabled"
        @update:model-value="update({ value: String($event) })"
      />
      <span
        v-else
        class="flex h-9 min-w-0 flex-1 items-center gap-1.5 rounded-md border border-dashed border-border px-2.5 text-xs text-muted-foreground"
        :data-testid="`plugin-variable-source-${index}`"
      >
        <component
          :is="row.source === 'credential' ? KeyRound : Users"
          :size="13"
          class="shrink-0"
        />
        <span class="truncate">{{
          row.source === 'credential'
            ? row.credentialName || 'Choose a credential'
            : 'Each workspace adds its own'
        }}</span>
      </span>
      <DropdownMenu v-if="row.secret">
        <DropdownMenuTrigger as-child>
          <Button
            size="icon-sm"
            variant="ghost"
            type="button"
            class="text-muted-foreground"
            :aria-label="`Where ${name} comes from`"
            :data-testid="`plugin-variable-menu-${index}`"
            :disabled="disabled"
            ><ChevronDown
          /></Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-80">
          <DropdownMenuItem @select="setSource('value')"
            >{{ saved ? 'Keep or replace the saved value' : 'Enter a value'
            }}<Check v-if="row.source === 'value'" class="ml-auto"
          /></DropdownMenuItem>
          <DropdownMenuItem @select="setSource('per_user')"
            >Each workspace adds its own<Check v-if="row.source === 'per_user'" class="ml-auto"
          /></DropdownMenuItem>
          <template v-if="choices.length">
            <DropdownMenuSeparator />
            <DropdownMenuLabel class="text-xs font-normal text-muted-foreground"
              >Use a credential</DropdownMenuLabel
            >
            <DropdownMenuItem
              v-for="credential in choices"
              :key="credential.id"
              @select="chooseCredential(credential)"
              ><span class="flex min-w-0 flex-col"
                ><span class="truncate">{{ credential.name }}</span
                ><span class="truncate text-xs text-muted-foreground">{{
                  credential.service_name
                }}</span></span
              ><Check
                v-if="row.source === 'credential' && row.credentialId === credential.id"
                class="ml-auto"
            /></DropdownMenuItem>
          </template>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
    <Button
      size="icon-sm"
      variant="ghost"
      type="button"
      :class="row.secret ? 'text-foreground' : 'text-muted-foreground'"
      :aria-pressed="row.secret"
      :aria-label="row.secret ? `${name} is secret` : `Make ${name} secret`"
      :title="row.secret ? 'Secret: kept as a credential, never shown again' : 'Make secret'"
      :disabled="disabled"
      @click="toggleSecret"
      ><Lock v-if="row.secret" /><LockOpen v-else
    /></Button>
    <Button
      size="icon-sm"
      variant="ghost"
      type="button"
      class="text-muted-foreground"
      :aria-label="`Remove ${name}`"
      :disabled="disabled"
      @click="emit('remove')"
      ><X
    /></Button>
  </div>
</template>
