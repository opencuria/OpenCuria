<!--
  PluginEditorDialog — create/edit an org-owned plugin with nested
  skills, MCP servers, and credential requirements.

  Requirements round-trip via `service_id` on edit (see lib/pluginForms).
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { Plus, Trash2, X } from '@lucide/vue'
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
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { useCredentialStore } from '@/stores/credentials'
import { useAuthStore } from '@/stores/auth'
import { usePluginStore } from '@/stores/plugins'
import type { Plugin } from '@/types'
import {
  emptyKeyValueRow,
  emptyMcpForm,
  emptyPluginForm,
  emptyRequirementForm,
  emptySkillForm,
  formToCreateIn,
  formToUpdateIn,
  PLACEHOLDER_EXAMPLE,
  pluginToForm,
  slugify,
  validatePluginForm,
  type PluginFormModel,
  type PluginMcpTransportOption,
  type PluginServiceTypeOption,
} from '@/lib/pluginForms'

const props = defineProps<{
  plugin: Plugin | null
}>()

const open = defineModel<boolean>('open', { default: false })

const pluginStore = usePluginStore()
const credentialStore = useCredentialStore()
const authStore = useAuthStore()
const servicesError = ref<string | null>(null)

const form: PluginFormModel = reactive(emptyPluginForm())
const submitting = ref(false)
const serverError = ref<string | null>(null)
const expandedSkill = ref<string | null>(null)
const expandedMcp = ref<string | null>(null)
const expandedReq = ref<string | null>(null)

const isEdit = computed(() => props.plugin !== null)
const title = computed(() => (isEdit.value ? 'Edit Plugin' : 'New Plugin'))

const transportOptions: Array<{ value: PluginMcpTransportOption; label: string }> = [
  { value: 'stdio', label: 'stdio (command)' },
  { value: 'streamable_http', label: 'streamable_http (URL)' },
  { value: 'sse', label: 'sse (URL)' },
]

const validationErrors = computed(() =>
  validatePluginForm({
    ...form,
    availableServices: credentialStore.servicesLoaded ? credentialStore.services : undefined,
  }),
)
const canSubmit = computed(
  () =>
    validationErrors.value.length === 0 &&
    credentialStore.servicesLoaded &&
    !credentialStore.servicesError &&
    !submitting.value,
)

function resetForm(): void {
  const fresh = props.plugin ? pluginToForm(props.plugin) : emptyPluginForm()
  form.name = fresh.name
  form.description = fresh.description
  form.enabled = fresh.enabled
  form.published = fresh.published
  form.skills = fresh.skills
  form.mcps = fresh.mcps
  form.requirements = fresh.requirements
  submitting.value = false
  serverError.value = null
  expandedSkill.value = form.skills[0]?.uid ?? null
  expandedMcp.value = form.mcps[0]?.uid ?? null
  expandedReq.value = form.requirements[0]?.uid ?? null
}

watch(
  () => [open.value, props.plugin?.id] as const,
  ([isOpen]) => {
    if (isOpen) {
      resetForm()
      void loadCredentialServices()
    }
  },
  { immediate: true },
)

async function loadCredentialServices(): Promise<void> {
  if (credentialStore.servicesLoaded || !authStore.activeOrganizationId) return
  servicesError.value = null
  await credentialStore.fetchServices()
  if (credentialStore.servicesError) servicesError.value = credentialStore.servicesError
}

function syncMcpOAuthRequirement(mcpUid: string, requirementKey: string): void {
  const mcp = form.mcps.find((entry) => entry.uid === mcpUid)
  if (!mcp) return
  mcp.oauthRequirementKey = requirementKey
  const requirement = form.requirements.find((entry) => entry.reqKey === requirementKey)
  const service = credentialStore.services.find((entry) => entry.id === requirement?.serviceId)
  if (service?.credential_type === 'mcp_oauth') mcp.url = service.oauth_server_url
}

function syncRequirementService(reqUid: string, serviceId: string): void {
  const req = form.requirements.find((r) => r.uid === reqUid)
  if (!req) return
  req.serviceId = serviceId
  const svc = credentialStore.services.find(
    (s) =>
      s.id === serviceId &&
      (s.organization_id === null || s.organization_id === authStore.activeOrganizationId),
  )
  if (svc) {
    req.credentialType = svc.credential_type as PluginServiceTypeOption
    if (!req.reqKey.trim()) req.reqKey = slugify(svc.name).replace(/-/g, '_')
    if (svc.credential_type === 'mcp_oauth') {
      let matchingServers = form.mcps.filter((item) => item.oauthRequirementKey === req.reqKey)
      const oauthRequirements = form.requirements.filter(
        (item) => item.credentialType === 'mcp_oauth',
      )
      const oauthServers = form.mcps.filter((item) => item.authType === 'oauth')
      if (!matchingServers.length && oauthRequirements.length === 1 && oauthServers.length === 1) {
        matchingServers = oauthServers
        matchingServers[0]!.oauthRequirementKey = req.reqKey
      }
      for (const server of matchingServers) server.url = svc.oauth_server_url
    }
  }
}

/**
 * Narrow transport switch: changing transports clears the now-unused
 * fields immediately so `mcpToIn` never ships stale values (stdio
 * forbids URL; http/sse forbid command/args, env/headers follow the
 * active transport).
 */
function addSkill(): void {
  form.skills.push(emptySkillForm())
  expandedSkill.value = form.skills[form.skills.length - 1]!.uid
}

function addMcp(): void {
  form.mcps.push(emptyMcpForm())
  expandedMcp.value = form.mcps[form.mcps.length - 1]!.uid
}

function addRequirement(): void {
  form.requirements.push(emptyRequirementForm())
  expandedReq.value = form.requirements[form.requirements.length - 1]!.uid
}

function syncMcpTransport(mcpUid: string, value: unknown): void {
  setMcpTransport(mcpUid, String(value) as PluginMcpTransportOption)
}

function setMcpAuthentication(mcpUid: string, value: unknown): void {
  const mcp = form.mcps.find((entry) => entry.uid === mcpUid)
  if (!mcp) return
  mcp.authType = String(value) === 'oauth' ? 'oauth' : 'none'
  if (mcp.authType === 'none') mcp.oauthRequirementKey = ''
}

function setMcpTransport(mcpUid: string, transport: PluginMcpTransportOption): void {
  const mcp = form.mcps.find((entry) => entry.uid === mcpUid)
  if (!mcp || mcp.transport === transport) return
  mcp.transport = transport
  if (transport === 'stdio') {
    mcp.url = ''
    mcp.headers = []
    mcp.authType = 'none'
    mcp.oauthRequirementKey = ''
  } else {
    mcp.desktop = 'none'
    mcp.command = ''
    mcp.argsText = ''
    mcp.env = []
  }
}

async function handleSubmit(): Promise<void> {
  if (!credentialStore.servicesLoaded || credentialStore.servicesError) return
  const errors = validatePluginForm({
    ...form,
    availableServices: credentialStore.services,
  })
  if (errors.length > 0) return
  submitting.value = true
  serverError.value = null
  try {
    if (isEdit.value && props.plugin) {
      const updated = await pluginStore.updatePlugin(props.plugin.id, formToUpdateIn(form))
      if (updated) open.value = false
      else serverError.value = 'Failed to update plugin.'
    } else {
      const created = await pluginStore.createPlugin(formToCreateIn(form))
      if (created) open.value = false
      else serverError.value = 'Failed to create plugin.'
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent
      class="max-w-[56rem]! sm:max-w-[56rem]! w-[calc(100vw-2rem)]!"
      data-testid="plugin-editor-dialog"
      aria-describedby="plugin-editor-description"
    >
      <DialogHeader>
        <DialogTitle>{{ title }}</DialogTitle>
        <DialogDescription id="plugin-editor-description">
          {{
            isEdit
              ? 'Update metadata, skills, MCP servers, and credential requirements.'
              : 'Create an organization plugin with skills, MCP servers, and credential requirements.'
          }}
        </DialogDescription>
      </DialogHeader>

      <DialogBody class="max-h-[70dvh] overflow-y-auto">
        <form id="plugin-editor-form" class="flex flex-col gap-6" @submit.prevent="handleSubmit">
          <div
            v-if="serverError"
            class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
            data-testid="plugin-editor-error"
          >
            {{ serverError }}
          </div>
          <div
            v-if="servicesError"
            role="alert"
            class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
            data-testid="plugin-services-error"
          >
            {{ servicesError }}
            <Button type="button" size="sm" variant="outline" @click="loadCredentialServices"
              >Retry</Button
            >
          </div>

          <!-- Metadata -->
          <section class="space-y-3" aria-label="Metadata">
            <h3 class="text-sm font-semibold text-foreground">Metadata</h3>
            <div class="space-y-2">
              <Label for="plugin-name">Name</Label>
              <Input
                id="plugin-name"
                v-model="form.name"
                placeholder="Playwright Helper"
                data-testid="plugin-name"
                :disabled="submitting"
              />
            </div>
            <div class="space-y-2">
              <Label for="plugin-description">Description</Label>
              <Textarea
                id="plugin-description"
                v-model="form.description"
                :rows="3"
                placeholder="What this plugin provides…"
                data-testid="plugin-description"
                :disabled="submitting"
              />
            </div>
            <div class="grid gap-3 sm:grid-cols-2">
              <div
                class="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
              >
                <Label for="plugin-published" class="cursor-pointer font-normal">Published</Label>
                <Switch
                  id="plugin-published"
                  v-model="form.published"
                  data-testid="plugin-published"
                  :disabled="submitting"
                />
              </div>
              <div
                class="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
              >
                <Label for="plugin-enabled" class="cursor-pointer font-normal">Enabled</Label>
                <Switch
                  id="plugin-enabled"
                  v-model="form.enabled"
                  data-testid="plugin-enabled"
                  :disabled="submitting"
                />
              </div>
            </div>
          </section>

          <!-- Skills -->
          <section class="space-y-3" aria-label="Skills">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-sm font-semibold text-foreground">
                Skills ({{ form.skills.length }})
              </h3>
              <Button
                size="sm"
                variant="outline"
                type="button"
                data-testid="plugin-add-skill"
                @click="addSkill"
              >
                <Plus /> Add skill
              </Button>
            </div>
            <p class="text-xs text-muted-foreground">
              Markdown fragments injected into harness prompts, in list order.
            </p>
            <div
              v-if="!form.skills.length"
              class="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground"
            >
              No skills yet.
            </div>
            <div
              v-for="(skill, index) in form.skills"
              :key="skill.uid"
              class="space-y-3 rounded-md border border-border p-3"
              :data-testid="`plugin-skill-${index}`"
            >
              <div class="flex items-center justify-between gap-2">
                <button
                  type="button"
                  class="text-sm font-medium text-foreground"
                  @click="expandedSkill = expandedSkill === skill.uid ? null : skill.uid"
                >
                  {{ skill.name.trim() || `Skill #${index + 1}` }}
                </button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  type="button"
                  class="text-destructive hover:text-destructive"
                  :data-testid="`plugin-remove-skill-${index}`"
                  :aria-label="`Remove skill ${index + 1}`"
                  @click="form.skills.splice(index, 1)"
                >
                  <Trash2 />
                </Button>
              </div>
              <div v-if="expandedSkill === skill.uid || expandedSkill === null" class="space-y-3">
                <div class="space-y-2">
                  <Label :for="`skill-name-${skill.uid}`">Name</Label>
                  <Input
                    :id="`skill-name-${skill.uid}`"
                    v-model="skill.name"
                    placeholder="Playwright basics"
                    :disabled="submitting"
                  />
                </div>
                <div class="space-y-2">
                  <Label :for="`skill-body-${skill.uid}`">Body (Markdown)</Label>
                  <Textarea
                    :id="`skill-body-${skill.uid}`"
                    v-model="skill.body"
                    :rows="5"
                    placeholder="Use the browser tool to…"
                    :disabled="submitting"
                  />
                </div>
              </div>
            </div>
          </section>

          <!-- MCP servers -->
          <section class="space-y-3" aria-label="MCP servers">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-sm font-semibold text-foreground">
                MCP servers ({{ form.mcps.length }})
              </h3>
              <Button
                size="sm"
                variant="outline"
                type="button"
                data-testid="plugin-add-mcp"
                @click="addMcp"
              >
                <Plus /> Add MCP server
              </Button>
            </div>
            <p class="text-xs text-muted-foreground">
              Runs inside the workspace; localhost means the workspace. stdio uses a single
              executable command with one argument per line (no shell).
            </p>
            <div
              v-if="!form.mcps.length"
              class="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground"
            >
              No MCP servers yet.
            </div>
            <div
              v-for="(mcp, index) in form.mcps"
              :key="mcp.uid"
              class="space-y-3 rounded-md border border-border p-3"
              :data-testid="`plugin-mcp-${index}`"
            >
              <div class="flex items-center justify-between gap-2">
                <button
                  type="button"
                  class="text-sm font-medium text-foreground"
                  @click="expandedMcp = expandedMcp === mcp.uid ? null : mcp.uid"
                >
                  {{ mcp.name.trim() || `MCP server #${index + 1}` }}
                </button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  type="button"
                  class="text-destructive hover:text-destructive"
                  :data-testid="`plugin-remove-mcp-${index}`"
                  :aria-label="`Remove MCP server ${index + 1}`"
                  @click="form.mcps.splice(index, 1)"
                >
                  <Trash2 />
                </Button>
              </div>
              <div v-if="expandedMcp === mcp.uid || expandedMcp === null" class="space-y-3">
                <div class="space-y-2">
                  <Label :for="`mcp-name-${mcp.uid}`">Name</Label>
                  <Input
                    :id="`mcp-name-${mcp.uid}`"
                    v-model="mcp.name"
                    placeholder="Playwright"
                    :disabled="submitting"
                  />
                </div>
                <div class="space-y-2">
                  <Label>Transport</Label>
                  <Select
                    :model-value="mcp.transport"
                    @update:model-value="syncMcpTransport(mcp.uid, $event)"
                  >
                    <SelectTrigger :data-testid="`plugin-mcp-transport-${index}`">
                      <SelectValue placeholder="Select transport" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem
                        v-for="opt in transportOptions"
                        :key="opt.value"
                        :value="opt.value"
                      >
                        {{ opt.label }}
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div v-if="mcp.transport !== 'stdio'" class="space-y-2">
                  <Label>Authentication</Label>
                  <Select
                    :model-value="mcp.authType"
                    @update:model-value="setMcpAuthentication(mcp.uid, $event)"
                  >
                    <SelectTrigger :data-testid="`plugin-mcp-auth-${index}`">
                      <SelectValue placeholder="Select authentication" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">None</SelectItem>
                      <SelectItem value="oauth">OAuth 2.0 (MCP)</SelectItem>
                    </SelectContent>
                  </Select>
                  <div v-if="mcp.authType === 'oauth'" class="space-y-2">
                    <Label>Required OAuth credential</Label>
                    <Select
                      :model-value="mcp.oauthRequirementKey"
                      @update:model-value="syncMcpOAuthRequirement(mcp.uid, String($event))"
                    >
                      <SelectTrigger :data-testid="`plugin-mcp-oauth-requirement-${index}`">
                        <SelectValue placeholder="Select an MCP OAuth requirement" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem
                          v-for="req in form.requirements.filter(
                            (entry) => entry.required && entry.credentialType === 'mcp_oauth',
                          )"
                          :key="req.uid"
                          :value="req.reqKey"
                        >
                          {{ req.reqKey || 'Unnamed requirement' }}
                        </SelectItem>
                      </SelectContent>
                    </Select>
                    <p class="text-xs text-muted-foreground">
                      Select a required MCP OAuth requirement backed by an existing active or
                      inactive OAuth service. The MCP endpoint must exactly match the service.
                    </p>
                  </div>
                </div>
                <div v-if="mcp.transport === 'stdio'" class="space-y-2">
                  <Label>Managed desktop</Label>
                  <Select
                    :model-value="mcp.desktop"
                    @update:model-value="mcp.desktop = $event as typeof mcp.desktop"
                  >
                    <SelectTrigger :data-testid="`plugin-mcp-desktop-${index}`">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="none">None</SelectItem>
                      <SelectItem value="server_start">Start with server</SelectItem>
                      <SelectItem value="first_tool">Start on first tool</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div v-if="mcp.transport === 'stdio'" class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <Label :for="`mcp-command-${mcp.uid}`">Command</Label>
                    <Input
                      :id="`mcp-command-${mcp.uid}`"
                      v-model="mcp.command"
                      placeholder="npx"
                      :disabled="submitting"
                    />
                    <p class="text-xs text-muted-foreground">
                      Single executable, no shell metacharacters or inline arguments.
                    </p>
                  </div>
                  <div class="space-y-2">
                    <Label :for="`mcp-cwd-${mcp.uid}`">Working directory</Label>
                    <Input
                      :id="`mcp-cwd-${mcp.uid}`"
                      v-model="mcp.cwd"
                      placeholder="/workspace"
                      :disabled="submitting"
                    />
                  </div>
                  <div class="space-y-2 sm:col-span-2">
                    <Label :for="`mcp-args-${mcp.uid}`">Arguments (one per line)</Label>
                    <Textarea
                      :id="`mcp-args-${mcp.uid}`"
                      v-model="mcp.argsText"
                      :rows="3"
                      placeholder="-y&#10;@playwright/mcp@latest"
                      :disabled="submitting"
                    />
                  </div>
                </div>
                <div v-else class="space-y-2">
                  <Label :for="`mcp-url-${mcp.uid}`">URL</Label>
                  <Input
                    :id="`mcp-url-${mcp.uid}`"
                    v-model="mcp.url"
                    type="url"
                    placeholder="https://mcp.example.com/mcp"
                    :disabled="submitting"
                  />
                </div>
                <div class="space-y-2">
                  <div class="flex items-center justify-between">
                    <Label>Environment — stdio only (injected into the process)</Label>
                    <Button
                      size="sm"
                      variant="ghost"
                      type="button"
                      @click="mcp.env.push(emptyKeyValueRow())"
                    >
                      <Plus /> Add row
                    </Button>
                  </div>
                  <div
                    v-for="(row, rowIndex) in mcp.env"
                    :key="row.uid"
                    class="grid grid-cols-[1fr_1fr_auto] gap-2"
                  >
                    <Input
                      v-model="row.key"
                      placeholder="KEY"
                      :aria-label="`env key ${rowIndex + 1}`"
                      :disabled="submitting"
                    />
                    <Input
                      v-model="row.value"
                      :placeholder="`value or ${PLACEHOLDER_EXAMPLE}`"
                      :aria-label="`env value ${rowIndex + 1}`"
                      :disabled="submitting"
                    />
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      type="button"
                      :aria-label="`Remove env row ${rowIndex + 1}`"
                      @click="mcp.env.splice(rowIndex, 1)"
                    >
                      <X />
                    </Button>
                  </div>
                  <p class="text-xs text-muted-foreground">
                    No secret values here — reference requirement keys as
                    &#123;&#123;credential.KEY&#125;&#125;, e.g. {{ PLACEHOLDER_EXAMPLE }}. Only
                    used for stdio; cleared automatically for http/sse.
                  </p>
                </div>
                <div v-if="mcp.transport !== 'stdio'" class="space-y-2">
                  <div class="flex items-center justify-between">
                    <Label>Headers — http/sse only (sent with requests)</Label>
                    <Button
                      size="sm"
                      variant="ghost"
                      type="button"
                      @click="mcp.headers.push(emptyKeyValueRow())"
                    >
                      <Plus /> Add row
                    </Button>
                  </div>
                  <div
                    v-for="(row, rowIndex) in mcp.headers"
                    :key="row.uid"
                    class="grid grid-cols-[1fr_1fr_auto] gap-2"
                  >
                    <Input
                      v-model="row.key"
                      placeholder="Authorization"
                      :aria-label="`header key ${rowIndex + 1}`"
                      :disabled="submitting"
                    />
                    <Input
                      v-model="row.value"
                      :placeholder="`Bearer token or ${PLACEHOLDER_EXAMPLE}`"
                      :aria-label="`header value ${rowIndex + 1}`"
                      :disabled="submitting"
                    />
                    <Button
                      size="icon-sm"
                      variant="ghost"
                      type="button"
                      :aria-label="`Remove header row ${rowIndex + 1}`"
                      @click="mcp.headers.splice(rowIndex, 1)"
                    >
                      <X />
                    </Button>
                  </div>
                  <p class="text-xs text-muted-foreground">
                    Reference requirement keys as &#123;&#123;credential.KEY&#125;&#125;, e.g.
                    {{ PLACEHOLDER_EXAMPLE }}.
                  </p>
                </div>
                <div class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <Label :for="`mcp-startup-${mcp.uid}`">Startup timeout (s)</Label>
                    <Input
                      :id="`mcp-startup-${mcp.uid}`"
                      v-model.number="mcp.startupTimeout"
                      type="number"
                      min="1"
                      max="600"
                      :disabled="submitting"
                    />
                  </div>
                  <div class="space-y-2">
                    <Label :for="`mcp-request-${mcp.uid}`">Request timeout (s)</Label>
                    <Input
                      :id="`mcp-request-${mcp.uid}`"
                      v-model.number="mcp.requestTimeout"
                      type="number"
                      min="1"
                      max="600"
                      :disabled="submitting"
                    />
                  </div>
                </div>
              </div>
            </div>
          </section>

          <!-- Credential requirements -->
          <section class="space-y-3" aria-label="Credential requirements">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-sm font-semibold text-foreground">
                Credential requirements ({{ form.requirements.length }})
              </h3>
              <Button
                size="sm"
                variant="outline"
                type="button"
                data-testid="plugin-add-requirement"
                @click="addRequirement"
              >
                <Plus /> Add requirement
              </Button>
            </div>
            <p class="text-xs text-muted-foreground">
              Select an existing credential service. Create organization services in Settings →
              Credential Services first. OAuth MCP endpoint must exactly match its service endpoint.
              Use the requirement key in env/headers as &#123;&#123;credential.KEY&#125;&#125;, e.g.
              {{ PLACEHOLDER_EXAMPLE }}.
            </p>
            <div
              v-if="!form.requirements.length"
              class="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground"
            >
              No credential requirements.
            </div>
            <div
              v-for="(req, index) in form.requirements"
              :key="req.uid"
              class="space-y-3 rounded-md border border-border p-3"
              :data-testid="`plugin-requirement-${index}`"
            >
              <div class="flex items-center justify-between gap-2">
                <button
                  type="button"
                  class="text-sm font-medium text-foreground"
                  @click="expandedReq = expandedReq === req.uid ? null : req.uid"
                >
                  {{ req.reqKey.trim() || `Requirement #${index + 1}` }}
                </button>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  type="button"
                  class="text-destructive hover:text-destructive"
                  :data-testid="`plugin-remove-requirement-${index}`"
                  :aria-label="`Remove requirement ${index + 1}`"
                  @click="form.requirements.splice(index, 1)"
                >
                  <Trash2 />
                </Button>
              </div>
              <div v-if="expandedReq === req.uid || expandedReq === null" class="space-y-3">
                <div class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <Label :for="`req-key-${req.uid}`">Key</Label>
                    <Input
                      :id="`req-key-${req.uid}`"
                      v-model="req.reqKey"
                      placeholder="api_key"
                      :disabled="submitting"
                    />
                  </div>
                  <div
                    class="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
                  >
                    <Label :for="`req-required-${req.uid}`" class="cursor-pointer font-normal"
                      >Required</Label
                    >
                    <Switch
                      :id="`req-required-${req.uid}`"
                      v-model="req.required"
                      :disabled="submitting"
                    />
                  </div>
                </div>
                <div class="space-y-2">
                  <Label :for="`req-desc-${req.uid}`">Description</Label>
                  <Input
                    :id="`req-desc-${req.uid}`"
                    v-model="req.description"
                    placeholder="Used for API access"
                    :disabled="submitting"
                  />
                </div>
                <div class="space-y-2">
                  <Label>Credential service</Label>
                  <Select
                    :model-value="req.serviceId"
                    @update:model-value="(value) => syncRequirementService(req.uid, String(value))"
                  >
                    <SelectTrigger :data-testid="`plugin-requirement-service-${index}`"
                      ><SelectValue placeholder="Select an existing service"
                    /></SelectTrigger>
                    <SelectContent
                      ><SelectItem
                        v-for="service in credentialStore.services.filter(
                          (item) =>
                            item.organization_id === null ||
                            item.organization_id === authStore.activeOrganizationId,
                        )"
                        :key="service.id"
                        :value="service.id"
                        >{{ service.name }} · {{ service.credential_type
                        }}{{ service.is_active ? '' : ' (inactive)' }}</SelectItem
                      ></SelectContent
                    >
                  </Select>
                  <p
                    v-if="req.credentialType === 'mcp_oauth' && req.serviceId"
                    class="break-all text-xs text-muted-foreground"
                  >
                    Fixed OAuth endpoint:
                    {{
                      credentialStore.services.find((service) => service.id === req.serviceId)
                        ?.oauth_server_url
                    }}. Match the MCP URL exactly.
                  </p>
                </div>
              </div>
            </div>
          </section>

          <div
            v-if="validationErrors.length"
            class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
            data-testid="plugin-editor-validation"
          >
            <ul class="list-disc space-y-0.5 pl-5">
              <li v-for="err in validationErrors" :key="err">{{ err }}</li>
            </ul>
          </div>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="submitting" @click="open = false">
          Cancel
        </Button>
        <Button
          type="submit"
          form="plugin-editor-form"
          data-testid="plugin-editor-save"
          :disabled="!canSubmit"
        >
          <LoadingSpinner v-if="submitting" :size="12" />
          <span v-else>{{ isEdit ? 'Save Changes' : 'Create Plugin' }}</span>
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
