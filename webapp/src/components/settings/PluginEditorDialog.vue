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

const serviceTypeOptions: Array<{ value: PluginServiceTypeOption; label: string }> = [
  { value: 'env', label: 'Environment Variable' },
  { value: 'file', label: 'Credential File' },
  { value: 'ssh_key', label: 'SSH Key Pair' },
]

const validationErrors = computed(() => validatePluginForm(form))
const canSubmit = computed(() => validationErrors.value.length === 0 && !submitting.value)

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
      if (!credentialStore.services.length) {
        void credentialStore.fetchServices()
      }
    }
  },
  { immediate: true },
)

function syncRequirementService(reqUid: string, serviceId: string): void {
  const req = form.requirements.find((r) => r.uid === reqUid)
  if (!req) return
  req.serviceId = serviceId
  const svc = credentialStore.services.find((s) => s.id === serviceId)
  if (svc) {
    req.serviceName = svc.name
    if (!req.reqKey.trim()) {
      req.reqKey = slugify(svc.name).replace(/-/g, '_')
    }
  }
}

/**
 * Narrow transport switch: changing transports clears the now-unused
 * fields immediately so `mcpToIn` never ships stale values (stdio
 * forbids URL; http/sse forbid command/args, env/headers follow the
 * active transport).
 */
function setMcpTransport(mcpUid: string, transport: PluginMcpTransportOption): void {
  const mcp = form.mcps.find((entry) => entry.uid === mcpUid)
  if (!mcp || mcp.transport === transport) return
  mcp.transport = transport
  if (transport === 'stdio') {
    mcp.url = ''
    mcp.headers = []
  } else {
    mcp.command = ''
    mcp.argsText = ''
    mcp.env = []
  }
}

async function handleSubmit(): Promise<void> {
  const errors = validatePluginForm(form)
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
          {{ isEdit ? 'Update metadata, skills, MCP servers, and credential requirements.' : 'Create an organization plugin with skills, MCP servers, and credential requirements.' }}
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
              <div class="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
                <Label for="plugin-published" class="cursor-pointer font-normal">Published</Label>
                <Switch id="plugin-published" v-model="form.published" data-testid="plugin-published" :disabled="submitting" />
              </div>
              <div class="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
                <Label for="plugin-enabled" class="cursor-pointer font-normal">Enabled</Label>
                <Switch id="plugin-enabled" v-model="form.enabled" data-testid="plugin-enabled" :disabled="submitting" />
              </div>
            </div>
          </section>

          <!-- Skills -->
          <section class="space-y-3" aria-label="Skills">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-sm font-semibold text-foreground">Skills ({{ form.skills.length }})</h3>
              <Button
                size="sm"
                variant="outline"
                type="button"
                data-testid="plugin-add-skill"
                @click="form.skills.push(emptySkillForm()); expandedSkill = form.skills[form.skills.length - 1]!.uid"
              >
                <Plus /> Add skill
              </Button>
            </div>
            <p class="text-xs text-muted-foreground">Markdown fragments injected into harness prompts, in list order.</p>
            <div v-if="!form.skills.length" class="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground">
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
                  <Input :id="`skill-name-${skill.uid}`" v-model="skill.name" placeholder="Playwright basics" :disabled="submitting" />
                </div>
                <div class="space-y-2">
                  <Label :for="`skill-body-${skill.uid}`">Body (Markdown)</Label>
                  <Textarea :id="`skill-body-${skill.uid}`" v-model="skill.body" :rows="5" placeholder="Use the browser tool to…" :disabled="submitting" />
                </div>
              </div>
            </div>
          </section>

          <!-- MCP servers -->
          <section class="space-y-3" aria-label="MCP servers">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-sm font-semibold text-foreground">MCP servers ({{ form.mcps.length }})</h3>
              <Button
                size="sm"
                variant="outline"
                type="button"
                data-testid="plugin-add-mcp"
                @click="form.mcps.push(emptyMcpForm()); expandedMcp = form.mcps[form.mcps.length - 1]!.uid"
              >
                <Plus /> Add MCP server
              </Button>
            </div>
            <p class="text-xs text-muted-foreground">
              Runs inside the workspace; localhost means the workspace. stdio uses a single
              executable command with one argument per line (no shell).
            </p>
            <div v-if="!form.mcps.length" class="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground">
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
                  <Input :id="`mcp-name-${mcp.uid}`" v-model="mcp.name" placeholder="Playwright" :disabled="submitting" />
                </div>
                <div class="space-y-2">
                  <Label>Transport</Label>
                  <Select :model-value="mcp.transport" @update:model-value="(v) => setMcpTransport(mcp.uid, String(v) as PluginMcpTransportOption)">
                    <SelectTrigger :data-testid="`plugin-mcp-transport-${index}`">
                      <SelectValue placeholder="Select transport" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem v-for="opt in transportOptions" :key="opt.value" :value="opt.value">
                        {{ opt.label }}
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div v-if="mcp.transport === 'stdio'" class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <Label :for="`mcp-command-${mcp.uid}`">Command</Label>
                    <Input :id="`mcp-command-${mcp.uid}`" v-model="mcp.command" placeholder="npx" :disabled="submitting" />
                    <p class="text-xs text-muted-foreground">Single executable, no shell metacharacters or inline arguments.</p>
                  </div>
                  <div class="space-y-2">
                    <Label :for="`mcp-cwd-${mcp.uid}`">Working directory</Label>
                    <Input :id="`mcp-cwd-${mcp.uid}`" v-model="mcp.cwd" placeholder="/workspace" :disabled="submitting" />
                  </div>
                  <div class="space-y-2 sm:col-span-2">
                    <Label :for="`mcp-args-${mcp.uid}`">Arguments (one per line)</Label>
                    <Textarea :id="`mcp-args-${mcp.uid}`" v-model="mcp.argsText" :rows="3" placeholder="-y&#10;@playwright/mcp@latest" :disabled="submitting" />
                  </div>
                </div>
                <div v-else class="space-y-2">
                  <Label :for="`mcp-url-${mcp.uid}`">URL</Label>
                  <Input :id="`mcp-url-${mcp.uid}`" v-model="mcp.url" type="url" placeholder="https://mcp.example.com/mcp" :disabled="submitting" />
                </div>
                <div class="space-y-2">
                  <div class="flex items-center justify-between">
                    <Label>Environment — stdio only (injected into the process)</Label>
                    <Button size="sm" variant="ghost" type="button" @click="mcp.env.push(emptyKeyValueRow())">
                      <Plus /> Add row
                    </Button>
                  </div>
                  <div v-for="(row, rowIndex) in mcp.env" :key="row.uid" class="grid grid-cols-[1fr_1fr_auto] gap-2">
                    <Input v-model="row.key" placeholder="KEY" :aria-label="`env key ${rowIndex + 1}`" :disabled="submitting" />
                    <Input v-model="row.value" :placeholder="`value or ${PLACEHOLDER_EXAMPLE}`" :aria-label="`env value ${rowIndex + 1}`" :disabled="submitting" />
                    <Button size="icon-sm" variant="ghost" type="button" :aria-label="`Remove env row ${rowIndex + 1}`" @click="mcp.env.splice(rowIndex, 1)">
                      <X />
                    </Button>
                  </div>
                  <p class="text-xs text-muted-foreground">No secret values here — reference requirement keys as &#123;&#123;credential.KEY&#125;&#125;, e.g. {{ PLACEHOLDER_EXAMPLE }}. Only used for stdio; cleared automatically for http/sse.</p>
                </div>
                <div v-if="mcp.transport !== 'stdio'" class="space-y-2">
                  <div class="flex items-center justify-between">
                    <Label>Headers — http/sse only (sent with requests)</Label>
                    <Button size="sm" variant="ghost" type="button" @click="mcp.headers.push(emptyKeyValueRow())">
                      <Plus /> Add row
                    </Button>
                  </div>
                  <div v-for="(row, rowIndex) in mcp.headers" :key="row.uid" class="grid grid-cols-[1fr_1fr_auto] gap-2">
                    <Input v-model="row.key" placeholder="Authorization" :aria-label="`header key ${rowIndex + 1}`" :disabled="submitting" />
                    <Input v-model="row.value" :placeholder="`Bearer token or ${PLACEHOLDER_EXAMPLE}`" :aria-label="`header value ${rowIndex + 1}`" :disabled="submitting" />
                    <Button size="icon-sm" variant="ghost" type="button" :aria-label="`Remove header row ${rowIndex + 1}`" @click="mcp.headers.splice(rowIndex, 1)">
                      <X />
                    </Button>
                  </div>
                  <p class="text-xs text-muted-foreground">Reference requirement keys as &#123;&#123;credential.KEY&#125;&#125;, e.g. {{ PLACEHOLDER_EXAMPLE }}.</p>
                </div>
                <div class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <Label :for="`mcp-startup-${mcp.uid}`">Startup timeout (s)</Label>
                    <Input :id="`mcp-startup-${mcp.uid}`" v-model.number="mcp.startupTimeout" type="number" min="1" max="600" :disabled="submitting" />
                  </div>
                  <div class="space-y-2">
                    <Label :for="`mcp-request-${mcp.uid}`">Request timeout (s)</Label>
                    <Input :id="`mcp-request-${mcp.uid}`" v-model.number="mcp.requestTimeout" type="number" min="1" max="600" :disabled="submitting" />
                  </div>
                </div>
              </div>
            </div>
          </section>

          <!-- Credential requirements -->
          <section class="space-y-3" aria-label="Credential requirements">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-sm font-semibold text-foreground">Credential requirements ({{ form.requirements.length }})</h3>
              <Button
                size="sm"
                variant="outline"
                type="button"
                data-testid="plugin-add-requirement"
                @click="form.requirements.push(emptyRequirementForm()); expandedReq = form.requirements[form.requirements.length - 1]!.uid"
              >
                <Plus /> Add requirement
              </Button>
            </div>
            <p class="text-xs text-muted-foreground">
              Reference an existing credential service, or define a new org service for this
              plugin. Workspaces must attach a matching credential before enabling the plugin.
              Use the requirement key in env/headers as &#123;&#123;credential.KEY&#125;&#125;, e.g. {{ PLACEHOLDER_EXAMPLE }}.
            </p>
            <div v-if="!form.requirements.length" class="rounded-md border border-dashed border-border px-4 py-3 text-sm text-muted-foreground">
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
                    <Input :id="`req-key-${req.uid}`" v-model="req.reqKey" placeholder="api_key" :disabled="submitting" />
                  </div>
                  <div class="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
                    <Label :for="`req-required-${req.uid}`" class="cursor-pointer font-normal">Required</Label>
                    <Switch :id="`req-required-${req.uid}`" v-model="req.required" :disabled="submitting" />
                  </div>
                </div>
                <div class="space-y-2">
                  <Label :for="`req-desc-${req.uid}`">Description</Label>
                  <Input :id="`req-desc-${req.uid}`" v-model="req.description" placeholder="Used for API access" :disabled="submitting" />
                </div>
                <div class="space-y-2">
                  <Label>Service source</Label>
                  <Select v-model="req.mode">
                    <SelectTrigger :data-testid="`plugin-requirement-mode-${index}`">
                      <SelectValue placeholder="Select source" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="existing">Existing service</SelectItem>
                      <SelectItem value="new">New service</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div v-if="req.mode === 'existing'" class="space-y-2">
                  <Label>Credential service</Label>
                  <Select :model-value="req.serviceId" @update:model-value="(v) => syncRequirementService(req.uid, String(v))">
                    <SelectTrigger :data-testid="`plugin-requirement-service-${index}`">
                      <SelectValue placeholder="Select a service" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem v-for="svc in credentialStore.services" :key="svc.id" :value="svc.id">
                        {{ svc.name }}
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div v-else class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <Label :for="`req-svc-name-${req.uid}`">Service name</Label>
                    <Input :id="`req-svc-name-${req.uid}`" v-model="req.serviceName" placeholder="Playwright Auth" :disabled="submitting" />
                  </div>
                  <div class="space-y-2">
                    <Label>Type</Label>
                    <Select v-model="req.credentialType">
                      <SelectTrigger>
                        <SelectValue placeholder="Select type" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem v-for="opt in serviceTypeOptions" :key="opt.value" :value="opt.value">
                          {{ opt.label }}
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div v-if="req.credentialType === 'env'" class="space-y-2">
                    <Label :for="`req-env-${req.uid}`">Environment variable</Label>
                    <Input :id="`req-env-${req.uid}`" v-model="req.envVarName" placeholder="PLAYWRIGHT_TOKEN" :disabled="submitting" />
                  </div>
                  <div v-if="req.credentialType === 'file'" class="space-y-2">
                    <Label :for="`req-path-${req.uid}`">Target path</Label>
                    <Input :id="`req-path-${req.uid}`" v-model="req.targetPath" placeholder="~/.config/auth.json" :disabled="submitting" />
                  </div>
                  <div class="space-y-2 sm:col-span-2">
                    <Label :for="`req-label-${req.uid}`">Label (optional)</Label>
                    <Input :id="`req-label-${req.uid}`" v-model="req.label" placeholder="API token" :disabled="submitting" />
                  </div>
                </div>
              </div>
            </div>
          </section>

          <div v-if="validationErrors.length" class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive" data-testid="plugin-editor-validation">
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
