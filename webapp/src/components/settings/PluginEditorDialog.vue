<!--
  PluginEditorDialog — create or edit a personal or organization plugin:
  name and description, skills, MCP servers and (advanced) further
  credentials. Every credential belongs to the plugin the same way: a
  credential service, optionally with the credential the plugin always
  uses. MCP servers can be pasted as JSON (Claude, Cursor, VS Code), a URL
  or a command line.
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ChevronRight, ClipboardPaste, KeyRound, Plus, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
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
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import PluginMcpServerEditor from './PluginMcpServerEditor.vue'
import type { CredentialOption } from './PluginVariableRow.vue'
import PluginSkillEditor from './PluginSkillEditor.vue'
import { useAuthStore } from '@/stores/auth'
import { useCredentialStore } from '@/stores/credentials'
import { usePluginStore } from '@/stores/plugins'
import type { Plugin } from '@opencuria/chat-core/plugins'
import {
  PLACEHOLDER_EXAMPLE,
  emptyMcpForm,
  emptyPluginForm,
  emptyRequirementForm,
  emptySkillForm,
  entryErrors,
  formToCreateIn,
  formToUpdateIn,
  generalErrors,
  mcpLabel,
  parseMcpConfig,
  pluginToForm,
  requirementLabel,
  setRequirementService,
  skillLabel,
  updateEntry,
  validatePluginForm,
  keepUsableCredentials,
  type PluginFormModel,
} from '@opencuria/chat-core/pluginForms'

const props = defineProps<{ plugin: Plugin | null }>()
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ saved: [plugin: Plugin] }>()

const pluginStore = usePluginStore()
const credentialStore = useCredentialStore()
const authStore = useAuthStore()

const form: PluginFormModel = reactive(emptyPluginForm())
const submitting = ref(false)
const attempted = ref(false)
const serverError = ref<string | null>(null)
const expanded = ref<string[]>([])
const importOpen = ref(false)
const importText = ref('')
const importError = ref<string | null>(null)
const advancedOpen = ref(false)

const isEdit = computed(() => props.plugin !== null)
const canShare = computed(() => !isEdit.value && authStore.isAdmin)
const services = computed(() =>
  credentialStore.services.filter(
    (service) =>
      service.organization_id === null ||
      service.organization_id === authStore.activeOrganizationId,
  ),
)
/** Credentials the plugin may use: organization plugins share organization ones only. */
const usableCredentials = computed<CredentialOption[]>(() =>
  credentialStore.credentials
    .filter((credential) => form.scope === 'personal' || credential.scope === 'organization')
    .map(({ id, name, service_id, service_name, credential_type }) => ({
      id,
      name,
      service_id,
      service_name,
      credential_type,
    })),
)
const valueCredentials = computed(() =>
  usableCredentials.value.filter((credential) => credential.credential_type === 'env'),
)
function credentialsOf(serviceId: string): CredentialOption[] {
  return serviceId ? usableCredentials.value.filter((entry) => entry.service_id === serviceId) : []
}
const errors = computed(() =>
  validatePluginForm({
    ...form,
    availableServices: credentialStore.servicesLoaded ? credentialStore.services : undefined,
  }),
)
const shownErrors = computed(() => (attempted.value ? errors.value : []))
const general = computed(() => generalErrors(shownErrors.value))

function reset(): void {
  const next = props.plugin ? pluginToForm(props.plugin) : emptyPluginForm()
  Object.assign(form, next)
  submitting.value = false
  attempted.value = false
  serverError.value = null
  importOpen.value = false
  importText.value = ''
  importError.value = null
  advancedOpen.value = next.requirements.length > 0
  // New plugins start with nothing expanded; existing ones show their content collapsed.
  expanded.value = []
}

watch(
  () => [open.value, props.plugin?.id] as const,
  ([isOpen]) => {
    if (!isOpen) return
    reset()
    if (!authStore.activeOrganizationId) return
    if (!credentialStore.servicesLoaded) void credentialStore.fetchServices()
    void credentialStore.fetchCredentials()
  },
  { immediate: true },
)
/** Organization plugins share organization credentials only. */
function setScope(value: unknown): void {
  form.scope = value === 'organization' ? 'organization' : 'personal'
  const usable = new Set(usableCredentials.value.map((credential) => credential.id))
  Object.assign(
    form,
    keepUsableCredentials(form, (id) => usable.has(id)),
  )
}

function isExpanded(uid: string): boolean {
  return expanded.value.includes(uid)
}
function setExpanded(uid: string, value: boolean): void {
  expanded.value = value
    ? [...expanded.value, uid]
    : expanded.value.filter((entry) => entry !== uid)
}

function addSkill(): void {
  const skill = emptySkillForm()
  form.skills.push(skill)
  setExpanded(skill.uid, true)
}
function addServer(): void {
  const server = emptyMcpForm()
  form.mcps.push(server)
  setExpanded(server.uid, true)
}
function importServers(): void {
  const result = parseMcpConfig(importText.value)
  if (result.error) {
    importError.value = result.error
    return
  }
  form.mcps.push(...result.servers)
  result.servers.forEach((server) => setExpanded(server.uid, true))
  importOpen.value = false
  importText.value = ''
  importError.value = null
}
function addRequirement(): void {
  form.requirements.push(emptyRequirementForm())
  advancedOpen.value = true
}
function setRequirement(uid: string, serviceId: unknown): void {
  const service = services.value.find((entry) => entry.id === serviceId)
  Object.assign(
    form,
    service
      ? setRequirementService(form, uid, service)
      : updateEntry(form, 'requirements', uid, (req) => ({ ...req, serviceId: String(serviceId) })),
  )
}
const EACH_WORKSPACE = '__each_workspace__'
function setRequirementCredential(uid: string, value: unknown): void {
  const credentialId = value === EACH_WORKSPACE ? '' : String(value ?? '')
  Object.assign(
    form,
    updateEntry(form, 'requirements', uid, (req) => ({ ...req, credentialId })),
  )
}

function setOpen(value: boolean): void {
  if (!submitting.value) open.value = value
}

async function save(): Promise<void> {
  if (submitting.value) return
  attempted.value = true
  if (errors.value.length) {
    // Reveal the entries that need attention.
    for (const [index, skill] of form.skills.entries())
      if (entryErrors(errors.value, skillLabel(index)).length) setExpanded(skill.uid, true)
    for (const [index, server] of form.mcps.entries())
      if (entryErrors(errors.value, mcpLabel(server, index)).length) setExpanded(server.uid, true)
    if (
      form.requirements.some((req, i) => entryErrors(errors.value, requirementLabel(req, i)).length)
    )
      advancedOpen.value = true
    return
  }
  submitting.value = true
  serverError.value = null
  try {
    const saved = props.plugin
      ? await pluginStore.updatePlugin(props.plugin.id, formToUpdateIn(form))
      : await pluginStore.createPlugin(formToCreateIn(form))
    if (saved) {
      emit('saved', saved)
      open.value = false
    } else serverError.value = pluginStore.lastError ?? 'The plugin could not be saved.'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="setOpen">
    <DialogContent
      class="max-w-[46rem]! sm:max-w-[46rem]! w-[calc(100vw-2rem)]!"
      data-testid="plugin-editor-dialog"
    >
      <DialogHeader>
        <DialogTitle>{{ isEdit ? 'Edit plugin' : 'New plugin' }}</DialogTitle>
        <DialogDescription>
          Give your agents skills and tools. Enable the plugin per workspace.
        </DialogDescription>
      </DialogHeader>

      <DialogBody class="max-h-[70dvh] overflow-y-auto">
        <form id="plugin-editor-form" class="flex flex-col gap-7" @submit.prevent="save">
          <div
            v-if="serverError"
            role="alert"
            class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
            data-testid="plugin-editor-error"
          >
            {{ serverError }}
          </div>

          <section class="space-y-4" aria-label="About">
            <div class="space-y-2">
              <Label for="plugin-name">Name</Label>
              <Input
                id="plugin-name"
                v-model="form.name"
                data-testid="plugin-name"
                :disabled="submitting"
              />
            </div>
            <div class="space-y-2">
              <Label for="plugin-description">Description</Label>
              <Textarea
                id="plugin-description"
                v-model="form.description"
                :rows="2"
                data-testid="plugin-description"
                :disabled="submitting"
              />
            </div>
            <div v-if="canShare" class="space-y-2">
              <Label>Who can use it</Label>
              <Tabs :model-value="form.scope" @update:model-value="setScope">
                <TabsList class="grid w-full grid-cols-2 sm:w-80">
                  <TabsTrigger value="personal" data-testid="plugin-scope-personal"
                    >Only me</TabsTrigger
                  >
                  <TabsTrigger value="organization" data-testid="plugin-scope-organization"
                    >Organization</TabsTrigger
                  >
                </TabsList>
              </Tabs>
            </div>
          </section>

          <section class="space-y-3" aria-label="Skills">
            <div class="flex items-end justify-between gap-3">
              <div class="space-y-0.5">
                <h3 class="text-sm font-semibold">Skills</h3>
                <p class="text-xs text-muted-foreground">
                  Instructions agents load when a task matches.
                </p>
              </div>
              <Button
                size="sm"
                variant="outline"
                type="button"
                data-testid="plugin-add-skill"
                :disabled="submitting"
                @click="addSkill"
                ><Plus /> Skill</Button
              >
            </div>
            <PluginSkillEditor
              v-for="(skill, index) in form.skills"
              :key="skill.uid"
              v-model="form.skills[index]!"
              :open="isExpanded(skill.uid)"
              :index="index"
              :errors="entryErrors(shownErrors, skillLabel(index))"
              :disabled="submitting"
              @update:open="setExpanded(skill.uid, $event)"
              @remove="form.skills.splice(index, 1)"
            />
          </section>

          <section class="space-y-3" aria-label="MCP servers">
            <div class="flex items-end justify-between gap-3">
              <div class="space-y-0.5">
                <h3 class="text-sm font-semibold">MCP servers</h3>
                <p class="text-xs text-muted-foreground">Tools agents can call.</p>
              </div>
              <div class="flex gap-2">
                <Button
                  size="sm"
                  variant="ghost"
                  type="button"
                  data-testid="plugin-import-mcp"
                  :disabled="submitting"
                  @click="importOpen = !importOpen"
                  ><ClipboardPaste /> Paste</Button
                >
                <Button
                  size="sm"
                  variant="outline"
                  type="button"
                  data-testid="plugin-add-mcp"
                  :disabled="submitting"
                  @click="addServer"
                  ><Plus /> Server</Button
                >
              </div>
            </div>
            <div
              v-if="importOpen"
              class="space-y-2 rounded-lg border border-dashed border-border bg-muted/30 p-3"
              data-testid="plugin-import-panel"
            >
              <Label for="plugin-import">Paste a configuration</Label>
              <Textarea
                id="plugin-import"
                v-model="importText"
                :rows="5"
                class="font-mono text-xs"
                spellcheck="false"
                data-testid="plugin-import-text"
              />
              <p class="text-xs text-muted-foreground">
                The <code>mcpServers</code> JSON from Claude, Cursor or VS Code, a server URL, or a
                command line.
              </p>
              <p v-if="importError" class="text-xs text-destructive" role="alert">
                {{ importError }}
              </p>
              <div class="flex justify-end gap-2">
                <Button size="sm" variant="ghost" type="button" @click="importOpen = false"
                  >Cancel</Button
                >
                <Button
                  size="sm"
                  type="button"
                  data-testid="plugin-import-apply"
                  :disabled="!importText.trim()"
                  @click="importServers"
                  >Add servers</Button
                >
              </div>
            </div>
            <PluginMcpServerEditor
              v-for="(server, index) in form.mcps"
              :key="server.uid"
              v-model="form.mcps[index]!"
              :open="isExpanded(server.uid)"
              :index="index"
              :errors="entryErrors(shownErrors, mcpLabel(server, index))"
              :requirements="form.requirements"
              :services="services"
              :credentials="valueCredentials"
              :disabled="submitting"
              @update:open="setExpanded(server.uid, $event)"
              @remove="form.mcps.splice(index, 1)"
            />
          </section>

          <Collapsible v-model:open="advancedOpen">
            <CollapsibleTrigger
              class="flex w-full items-center gap-2 text-left text-sm font-semibold focus-visible:outline-none"
              data-testid="plugin-advanced"
            >
              <ChevronRight
                :size="15"
                class="text-muted-foreground transition-transform"
                :class="advancedOpen ? 'rotate-90' : undefined"
              />
              More credentials
              <span class="text-xs font-normal text-muted-foreground">Advanced</span>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <div class="mt-3 space-y-3">
                <p class="text-xs text-muted-foreground">
                  Credentials the plugin needs besides its secret variables, such as an OAuth
                  sign-in. Choose one the plugin always uses, or let each workspace add its own. Use
                  it in a value as <code>{{ PLACEHOLDER_EXAMPLE }}</code
                  >.
                </p>
                <div
                  v-for="(req, index) in form.requirements"
                  :key="req.uid"
                  class="space-y-2 rounded-lg border border-border bg-card p-3"
                  :data-testid="`plugin-requirement-${index}`"
                >
                  <div class="grid gap-2 sm:grid-cols-2">
                    <Select
                      :model-value="req.serviceId"
                      @update:model-value="setRequirement(req.uid, $event)"
                    >
                      <SelectTrigger
                        class="w-full"
                        :data-testid="`plugin-requirement-service-${index}`"
                        ><SelectValue placeholder="Credential service"
                      /></SelectTrigger>
                      <SelectContent>
                        <SelectItem
                          v-for="service in services"
                          :key="service.id"
                          :value="service.id"
                          >{{ service.name
                          }}{{
                            service.credential_type === 'mcp_oauth' ? ' · OAuth' : ''
                          }}</SelectItem
                        >
                      </SelectContent>
                    </Select>
                    <Select
                      :model-value="req.credentialId || EACH_WORKSPACE"
                      :disabled="submitting || !req.serviceId"
                      @update:model-value="setRequirementCredential(req.uid, $event)"
                    >
                      <SelectTrigger
                        class="w-full"
                        :aria-label="`Credential for ${req.reqKey || 'this service'}`"
                        :data-testid="`plugin-requirement-credential-${index}`"
                        ><SelectValue
                      /></SelectTrigger>
                      <SelectContent>
                        <SelectItem :value="EACH_WORKSPACE">Each workspace adds its own</SelectItem>
                        <SelectItem
                          v-for="credential in credentialsOf(req.serviceId)"
                          :key="credential.id"
                          :value="credential.id"
                          >Always use “{{ credential.name }}”</SelectItem
                        >
                      </SelectContent>
                    </Select>
                  </div>
                  <div class="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-center">
                    <Input
                      v-model="req.reqKey"
                      class="font-mono text-xs"
                      aria-label="Key"
                      placeholder="key"
                      :disabled="submitting"
                    />
                    <div class="flex items-center gap-2">
                      <Switch
                        v-model="req.required"
                        :aria-label="`Required: ${req.reqKey || 'credential'}`"
                        :disabled="submitting || req.credentialType === 'mcp_oauth'"
                      />
                      <span class="text-xs text-muted-foreground">Required</span>
                      <Button
                        size="icon-sm"
                        variant="ghost"
                        type="button"
                        class="text-muted-foreground hover:text-destructive"
                        :aria-label="`Remove ${req.reqKey || 'credential'}`"
                        @click="form.requirements.splice(index, 1)"
                        ><Trash2
                      /></Button>
                    </div>
                  </div>
                  <ul
                    v-if="entryErrors(shownErrors, requirementLabel(req, index)).length"
                    class="text-xs text-destructive"
                  >
                    <li
                      v-for="error in entryErrors(shownErrors, requirementLabel(req, index))"
                      :key="error"
                    >
                      {{ error }}
                    </li>
                  </ul>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  type="button"
                  data-testid="plugin-add-requirement"
                  :disabled="submitting"
                  @click="addRequirement"
                  ><KeyRound /> Credential</Button
                >
              </div>
            </CollapsibleContent>
          </Collapsible>

          <ul
            v-if="general.length"
            class="list-disc space-y-0.5 rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 pl-8 text-sm text-destructive"
            data-testid="plugin-editor-validation"
          >
            <li v-for="error in general" :key="error">{{ error }}</li>
          </ul>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="submitting" @click="open = false"
          >Cancel</Button
        >
        <Button
          type="submit"
          form="plugin-editor-form"
          data-testid="plugin-editor-save"
          :disabled="submitting"
        >
          <LoadingSpinner v-if="submitting" :size="12" />
          <span v-else>{{ isEdit ? 'Save' : 'Create plugin' }}</span>
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
