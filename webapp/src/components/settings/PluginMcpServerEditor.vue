<!--
  PluginMcpServerEditor — one MCP server of a plugin draft. A server either
  runs as a command inside the workspace (one command line, environment
  variables) or is reached by URL (headers, optional OAuth sign-in). Secret
  values are kept as credentials of the plugin and never shown again.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { ChevronRight, Globe, Plus, Terminal, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import PluginVariableRow, { type CredentialOption } from './PluginVariableRow.vue'
import type { CredentialService } from '@opencuria/chat-core/plugins'
import {
  emptyPluginForm,
  emptyVariableRow,
  setMcpAuthentication,
  setMcpKind,
  setMcpOAuthRequirement,
  type McpKind,
  type PluginFormModel,
  type PluginMcpForm,
  type PluginRequirementForm,
  type VariableRow,
} from '@opencuria/chat-core/pluginForms'

const props = defineProps<{
  index: number
  errors: string[]
  requirements: PluginRequirementForm[]
  services: CredentialService[]
  /** Credentials a secret may use (already filtered for the plugin). */
  credentials: CredentialOption[]
  disabled?: boolean
}>()
const server = defineModel<PluginMcpForm>({ required: true })
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ remove: [] }>()

const isCommand = computed(() => server.value.kind === 'command')
const title = computed(() => server.value.name.trim() || 'New MCP server')
const summary = computed(() =>
  isCommand.value ? server.value.commandLine.trim() : server.value.url.trim(),
)
const oauthRequirements = computed(() =>
  props.requirements.filter((entry) => entry.required && entry.credentialType === 'mcp_oauth'),
)
const field = (name: string) => `mcp-${name}-${server.value.uid}`

/** Runs a shared chat-core form edit on this server alone. */
function edit(apply: (form: PluginFormModel) => PluginFormModel): void {
  const form = { ...emptyPluginForm(), requirements: props.requirements, mcps: [server.value] }
  server.value = apply(form).mcps[0]!
}

function update(patch: Partial<PluginMcpForm>): void {
  server.value = { ...server.value, ...patch }
}

function setKind(kind: unknown): void {
  edit((form) => setMcpKind(form, server.value.uid, kind === 'url' ? 'url' : 'command'))
}

function setAuth(value: unknown): void {
  edit((form) => setMcpAuthentication(form, server.value.uid, value === 'oauth' ? 'oauth' : 'none'))
}

function setOAuthRequirement(key: unknown): void {
  edit((form) => setMcpOAuthRequirement(form, server.value.uid, String(key), props.services))
}

function setVariable(uid: string, next: VariableRow): void {
  update({ variables: server.value.variables.map((row) => (row.uid === uid ? next : row)) })
}

function addVariable(): void {
  update({ variables: [...server.value.variables, emptyVariableRow()] })
}

function removeVariable(uid: string): void {
  update({ variables: server.value.variables.filter((row) => row.uid !== uid) })
}

const kinds: Array<{ value: McpKind; label: string }> = [
  { value: 'command', label: 'Command' },
  { value: 'url', label: 'URL' },
]
</script>

<template>
  <Collapsible
    v-model:open="open"
    class="rounded-lg border border-border bg-card"
    :class="errors.length && !open ? 'border-destructive/40' : undefined"
    :data-testid="`plugin-mcp-${index}`"
  >
    <div class="flex items-center gap-2 px-3 py-2">
      <CollapsibleTrigger
        class="flex min-w-0 flex-1 items-center gap-2 rounded-md py-1 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ChevronRight
          :size="15"
          class="shrink-0 text-muted-foreground transition-transform"
          :class="open ? 'rotate-90' : undefined"
        />
        <component
          :is="isCommand ? Terminal : Globe"
          :size="15"
          class="shrink-0 text-muted-foreground"
        />
        <span class="min-w-0 truncate text-sm font-medium">{{ title }}</span>
        <span
          v-if="!open && summary"
          class="hidden min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground sm:block"
          >{{ summary }}</span
        >
        <span v-if="!open && errors.length" class="ml-auto shrink-0 text-xs text-destructive">{{
          errors[0]
        }}</span>
      </CollapsibleTrigger>
      <Button
        size="icon-sm"
        variant="ghost"
        type="button"
        class="text-muted-foreground hover:text-destructive"
        :aria-label="`Remove ${title}`"
        :data-testid="`plugin-remove-mcp-${index}`"
        :disabled="disabled"
        @click="emit('remove')"
        ><Trash2
      /></Button>
    </div>
    <CollapsibleContent>
      <div class="space-y-4 border-t border-border px-4 py-4">
        <div class="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
          <div class="space-y-2">
            <Label :for="field('name')">Name</Label>
            <Input
              :id="field('name')"
              :model-value="server.name"
              :disabled="disabled"
              @update:model-value="update({ name: String($event) })"
            />
          </div>
          <Tabs :model-value="server.kind" @update:model-value="setKind">
            <TabsList class="grid w-full grid-cols-2 sm:w-48" :aria-label="`${title} type`">
              <TabsTrigger
                v-for="kind in kinds"
                :key="kind.value"
                :value="kind.value"
                :disabled="disabled"
                :data-testid="`plugin-mcp-kind-${kind.value}-${index}`"
                >{{ kind.label }}</TabsTrigger
              >
            </TabsList>
          </Tabs>
        </div>

        <div v-if="isCommand" class="space-y-2">
          <Label :for="field('command')">Command</Label>
          <Input
            :id="field('command')"
            :model-value="server.commandLine"
            class="font-mono text-xs"
            spellcheck="false"
            autocomplete="off"
            :disabled="disabled"
            :data-testid="`plugin-mcp-command-${index}`"
            @update:model-value="update({ commandLine: String($event) })"
          />
          <p class="text-xs text-muted-foreground">
            Runs inside the workspace without a shell, e.g. <code>npx -y &lt;package&gt;</code>.
          </p>
        </div>
        <div v-else class="grid gap-4 sm:grid-cols-[1fr_11rem]">
          <div class="space-y-2">
            <Label :for="field('url')">URL</Label>
            <Input
              :id="field('url')"
              :model-value="server.url"
              type="url"
              class="font-mono text-xs"
              spellcheck="false"
              :disabled="disabled || server.authType === 'oauth'"
              :data-testid="`plugin-mcp-url-${index}`"
              @update:model-value="update({ url: String($event) })"
            />
          </div>
          <div class="space-y-2">
            <Label>Transport</Label>
            <Select
              :model-value="server.transport"
              :disabled="disabled"
              @update:model-value="
                update({ transport: $event === 'sse' ? 'sse' : 'streamable_http' })
              "
            >
              <SelectTrigger class="w-full" :data-testid="`plugin-mcp-transport-${index}`"
                ><SelectValue
              /></SelectTrigger>
              <SelectContent>
                <SelectItem value="streamable_http">Streamable HTTP</SelectItem>
                <SelectItem value="sse">SSE</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <div
          v-if="!isCommand && (oauthRequirements.length || server.authType === 'oauth')"
          class="space-y-2"
        >
          <Label>Sign-in</Label>
          <div class="grid gap-2 sm:grid-cols-2">
            <Select
              :model-value="server.authType"
              :disabled="disabled"
              @update:model-value="setAuth"
            >
              <SelectTrigger class="w-full" :data-testid="`plugin-mcp-auth-${index}`"
                ><SelectValue
              /></SelectTrigger>
              <SelectContent>
                <SelectItem value="none">None</SelectItem>
                <SelectItem value="oauth">OAuth (each user signs in)</SelectItem>
              </SelectContent>
            </Select>
            <Select
              v-if="server.authType === 'oauth'"
              :model-value="server.oauthRequirementKey"
              :disabled="disabled"
              @update:model-value="setOAuthRequirement"
            >
              <SelectTrigger class="w-full" :data-testid="`plugin-mcp-oauth-requirement-${index}`"
                ><SelectValue placeholder="OAuth credential"
              /></SelectTrigger>
              <SelectContent>
                <SelectItem v-for="req in oauthRequirements" :key="req.uid" :value="req.reqKey">{{
                  req.reqKey || 'Unnamed credential'
                }}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <div class="space-y-2">
          <div class="flex items-center justify-between gap-2">
            <Label>{{ isCommand ? 'Environment variables' : 'Headers' }}</Label>
            <Button
              size="xs"
              variant="ghost"
              type="button"
              :disabled="disabled"
              :data-testid="`plugin-mcp-add-variable-${index}`"
              @click="addVariable"
              ><Plus /> Add</Button
            >
          </div>
          <p v-if="!server.variables.length" class="text-xs text-muted-foreground">
            {{
              isCommand
                ? 'None. Mark API keys as secret: they are kept as credentials.'
                : 'None. Add an Authorization header as a secret if the server needs one.'
            }}
          </p>
          <PluginVariableRow
            v-for="(row, rowIndex) in server.variables"
            :key="row.uid"
            :model-value="row"
            :label="isCommand ? 'Variable' : 'Header'"
            :index="rowIndex"
            :credentials="credentials"
            :disabled="disabled"
            @update:model-value="setVariable(row.uid, $event)"
            @remove="removeVariable(row.uid)"
          />
        </div>
        <ul v-if="errors.length" class="space-y-0.5 text-xs text-destructive">
          <li v-for="error in errors" :key="error">{{ error }}</li>
        </ul>
      </div>
    </CollapsibleContent>
  </Collapsible>
</template>
