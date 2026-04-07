<script setup lang="ts">
/**
 * AgentDefinitionModal — Full-featured modal for creating and editing
 * org-specific agent definitions.
 *
 * Layout (large screens):
 *   Left column  — Basic settings: name, description, multi-chat, env vars,
 *                  available options.
 *   Right column — Commands: configure phase (0+), run_first phase (optional),
 *                  run phase (exactly 1).
 *
 * Tab 2 — Credentials: link credential services, set per-service default env
 *   and commands.
 */
import { ref, computed, watch } from 'vue'
import { get, post, patch, del } from '@/services/api'
import {
  UiButton,
  UiDialog,
  UiInput,
  UiSpinner,
  UiTextarea,
} from '@/components/ui'
import {
  Plus,
  Trash2,
  GripVertical,
  ChevronDown,
  ChevronUp,
  Key,
  X,
  Check,
  AlertTriangle,
  Bot,
  Settings2,
  Zap,
} from 'lucide-vue-next'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface EnvPair {
  key: string
  value: string
}

interface CommandRow {
  id?: string
  phase: 'configure' | 'run' | 'run_first'
  commandText: string
  workdir: string
  env: EnvPair[]
  description: string
  order: number
  _dragId: number
}

interface AgentCommand {
  id?: string
  phase: string
  args: string[]
  workdir?: string | null
  env: Record<string, string>
  description: string
  order: number
}

interface AvailableOption {
  key: string
  label: string
  choicesText: string // comma-separated string for UI
  default: string
  _id: number
}

interface CredentialRelation {
  id?: string
  credential_service_id: string
  credential_service_name?: string
  default_env: EnvPair[]
  commands: CommandRow[]
}

interface CredentialServiceOption {
  id: string
  name: string
  slug: string
  description: string
  credential_type: string
  env_var_name: string
}

interface AgentDefinitionFull {
  id: string
  name: string
  description: string
  is_standard: boolean
  organization_id: string | null
  available_options: {
    key: string
    label: string
    choices: string[]
    default: string
  }[]
  default_env: Record<string, string>
  supports_multi_chat: boolean
  required_credential_service_ids: string[]
  commands: AgentCommand[]
  is_active: boolean
}

// ---------------------------------------------------------------------------
// Props / emits
// ---------------------------------------------------------------------------

const props = withDefaults(
  defineProps<{
    open: boolean
    agent?: AgentDefinitionFull | null
    credentialServices: CredentialServiceOption[]
  }>(),
  {
    agent: null,
  },
)

const emit = defineEmits<{
  'update:open': [value: boolean]
  saved: [agent: AgentDefinitionFull]
}>()

// ---------------------------------------------------------------------------
// Dynamic placeholder validation
// ---------------------------------------------------------------------------

const availableOptions = ref<AvailableOption[]>([])

const validPlaceholders = computed(() => {
  const base = new Set(['{prompt}', '{workdir}', '{chat_id}'])
  for (const opt of availableOptions.value) {
    if (opt.key.trim()) base.add(`{${opt.key.trim()}}`)
  }
  return base
})

function validatePlaceholders(text: string): { valid: string[]; invalid: string[] } {
  const matches = text.match(/\{[^}]+\}/g) ?? []
  const valid: string[] = []
  const invalid: string[] = []
  for (const m of matches) {
    if (validPlaceholders.value.has(m)) valid.push(m)
    else invalid.push(m)
  }
  return { valid, invalid }
}

// ---------------------------------------------------------------------------
// Form state
// ---------------------------------------------------------------------------

const activeTab = ref<'definition' | 'credentials'>('definition')

// --- Definition tab ---
const name = ref('')
const description = ref('')
const defaultEnv = ref<EnvPair[]>([])
const commands = ref<CommandRow[]>([])
const supportsMultiChat = ref(false)

// --- Credentials tab ---
const credentialRelations = ref<CredentialRelation[]>([])
const expandedRelation = ref<string | null>(null)

// --- State ---
const loading = ref(false)
const error = ref<string | null>(null)
let _dragCounter = 0
let _dragId = 0
let _dragFromPhase: string = ''
let _optId = 0

// ---------------------------------------------------------------------------
// Available Options management
// ---------------------------------------------------------------------------

function optionsFromApi(opts: AgentDefinitionFull['available_options']): AvailableOption[] {
  return (opts || []).map((o) => ({
    key: o.key || '',
    label: o.label || '',
    choicesText: (o.choices || []).join(', '),
    default: o.default || '',
    _id: _optId++,
  }))
}

function optionsToApi() {
  return availableOptions.value
    .filter((o) => o.key.trim())
    .map((o) => ({
      key: o.key.trim(),
      label: o.label.trim(),
      choices: o.choicesText
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean),
      default: o.default.trim(),
    }))
}

function addOption() {
  availableOptions.value.push({ key: '', label: '', choicesText: '', default: '', _id: _optId++ })
}

function removeOption(i: number) {
  availableOptions.value.splice(i, 1)
}

// ---------------------------------------------------------------------------
// Helpers — convert between API format and UI format
// ---------------------------------------------------------------------------

function argsToText(args: string[]): string {
  if (!args || !args.length) return ''
  return args
    .map((a) => (a.includes(' ') || a.includes('"') ? JSON.stringify(a) : a))
    .join(' ')
}

function textToArgs(text: string): string[] {
  const args: string[] = []
  let current = ''
  let inSingle = false
  let inDouble = false

  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (ch === "'" && !inDouble) {
      inSingle = !inSingle
    } else if (ch === '"' && !inSingle) {
      inDouble = !inDouble
    } else if (ch === ' ' && !inSingle && !inDouble) {
      if (current) {
        args.push(current)
        current = ''
      }
    } else {
      current += ch
    }
  }
  if (current) args.push(current)
  return args
}

function envObjToPairs(obj: Record<string, string>): EnvPair[] {
  return Object.entries(obj || {}).map(([key, value]) => ({ key, value }))
}

function pairsToEnvObj(pairs: EnvPair[]): Record<string, string> {
  const obj: Record<string, string> = {}
  for (const { key, value } of pairs) {
    if (key.trim()) obj[key.trim()] = value
  }
  return obj
}

function apiCommandsToRows(apiCommands: AgentCommand[]): CommandRow[] {
  return (apiCommands || []).map((cmd) => ({
    id: cmd.id,
    phase: cmd.phase as 'configure' | 'run' | 'run_first',
    commandText: argsToText(cmd.args),
    workdir: cmd.workdir || '/workspace',
    env: envObjToPairs(cmd.env || {}),
    description: cmd.description || '',
    order: cmd.order,
    _dragId: _dragCounter++,
  }))
}

function rowsToApiCommands(rows: CommandRow[]): AgentCommand[] {
  return rows.map((row, i) => ({
    phase: row.phase,
    args: textToArgs(row.commandText),
    workdir: row.workdir || '/workspace',
    env: pairsToEnvObj(row.env),
    description: row.description,
    order: i,
  }))
}

// ---------------------------------------------------------------------------
// Load credential relations
// ---------------------------------------------------------------------------

async function loadCredentialRelations(agentId: string) {
  try {
    const relations = await get<
      {
        id: string
        credential_service_id: string
        credential_service_name: string
        default_env: Record<string, string>
        commands: AgentCommand[]
      }[]
    >(`/org-agent-definitions/${agentId}/credential-relations/`)
    credentialRelations.value = relations.map((r) => ({
      id: r.id,
      credential_service_id: r.credential_service_id,
      credential_service_name: r.credential_service_name,
      default_env: envObjToPairs(r.default_env || {}),
      commands: apiCommandsToRows(r.commands || []),
    }))
  } catch {
    credentialRelations.value = []
  }
}

function ensureRequiredCredentialRelations(requiredIds: string[]) {
  const existing = new Set(credentialRelations.value.map((r) => r.credential_service_id))
  for (const serviceId of requiredIds) {
    if (existing.has(serviceId)) continue
    const svc = props.credentialServices.find((s) => s.id === serviceId)
    credentialRelations.value.push({
      credential_service_id: serviceId,
      credential_service_name: svc?.name || serviceId,
      default_env: [],
      commands: [],
    })
  }
}

// ---------------------------------------------------------------------------
// Initialize form when dialog opens / agent changes
// ---------------------------------------------------------------------------

watch(
  () => props.open,
  async (isOpen) => {
    if (!isOpen) return
    activeTab.value = 'definition'
    error.value = null

    if (props.agent) {
      name.value = props.agent.name
      description.value = props.agent.description || ''
      defaultEnv.value = envObjToPairs(props.agent.default_env || {})
      commands.value = apiCommandsToRows(props.agent.commands || [])
      supportsMultiChat.value = props.agent.supports_multi_chat
      availableOptions.value = optionsFromApi(props.agent.available_options || [])
      await loadCredentialRelations(props.agent.id)
      ensureRequiredCredentialRelations(props.agent.required_credential_service_ids || [])
    } else {
      // New agent defaults
      name.value = ''
      description.value = ''
      defaultEnv.value = []
      supportsMultiChat.value = false
      availableOptions.value = []
      commands.value = [
        {
          phase: 'run',
          commandText: 'my-agent --prompt {prompt}',
          workdir: '/workspace',
          env: [],
          description: 'Run agent',
          order: 0,
          _dragId: _dragCounter++,
        },
      ]
      credentialRelations.value = []
    }
  },
  { immediate: true },
)

// ---------------------------------------------------------------------------
// Default env management
// ---------------------------------------------------------------------------

function addEnvPair() {
  defaultEnv.value.push({ key: '', value: '' })
}

function removeEnvPair(i: number) {
  defaultEnv.value.splice(i, 1)
}

// ---------------------------------------------------------------------------
// Command management
// ---------------------------------------------------------------------------

const configureCommands = computed(() => commands.value.filter((c) => c.phase === 'configure'))

const runFirstCommand = computed(() =>
  commands.value.find((c) => c.phase === 'run_first') || null,
)

const runCommands = computed(() => commands.value.filter((c) => c.phase === 'run'))

function addConfigureCommand() {
  commands.value.push({
    phase: 'configure',
    commandText: '',
    workdir: '/workspace',
    env: [],
    description: '',
    order: configureCommands.value.length,
    _dragId: _dragCounter++,
  })
}

function enableRunFirst() {
  commands.value.push({
    phase: 'run_first',
    commandText: '',
    workdir: '/workspace',
    env: [],
    description: 'First message init',
    order: 0,
    _dragId: _dragCounter++,
  })
}

function disableRunFirst() {
  const idx = commands.value.findIndex((c) => c.phase === 'run_first')
  if (idx !== -1) commands.value.splice(idx, 1)
}

function addRunCommand() {
  commands.value.push({
    phase: 'run',
    commandText: '',
    workdir: '/workspace',
    env: [],
    description: '',
    order: runCommands.value.length,
    _dragId: _dragCounter++,
  })
}

function removeCommand(row: CommandRow) {
  const idx = commands.value.findIndex((c) => c._dragId === row._dragId)
  if (idx !== -1) commands.value.splice(idx, 1)
}

function addCommandEnvPair(row: CommandRow) {
  row.env.push({ key: '', value: '' })
}

function removeCommandEnvPair(row: CommandRow, i: number) {
  row.env.splice(i, 1)
}

// Drag-and-drop for commands within a phase
function onDragStart(e: DragEvent, row: CommandRow) {
  _dragId = row._dragId
  _dragFromPhase = row.phase
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
}

function onDragOver(e: DragEvent, row: CommandRow) {
  if (row.phase !== _dragFromPhase) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
}

function onDrop(e: DragEvent, targetRow: CommandRow) {
  if (targetRow.phase !== _dragFromPhase) return
  e.preventDefault()

  const fromIndex = commands.value.findIndex((c) => c._dragId === _dragId)
  const toIndex = commands.value.findIndex((c) => c._dragId === targetRow._dragId)
  if (fromIndex === -1 || toIndex === -1 || fromIndex === toIndex) return

  const [moved] = commands.value.splice(fromIndex, 1)
  if (!moved) return
  commands.value.splice(toIndex, 0, moved)
}

// ---------------------------------------------------------------------------
// Credential relation management
// ---------------------------------------------------------------------------

const linkedCredentialServiceIds = computed(() =>
  credentialRelations.value.map((r) => r.credential_service_id),
)

function addCredentialRelation(svc: CredentialServiceOption) {
  if (linkedCredentialServiceIds.value.includes(svc.id)) return
  credentialRelations.value.push({
    credential_service_id: svc.id,
    credential_service_name: svc.name,
    default_env: [],
    commands: [],
  })
  expandedRelation.value = svc.id
}

function removeCredentialRelation(serviceId: string) {
  const idx = credentialRelations.value.findIndex((r) => r.credential_service_id === serviceId)
  if (idx !== -1) credentialRelations.value.splice(idx, 1)
  if (expandedRelation.value === serviceId) expandedRelation.value = null
}

function toggleRelationExpand(serviceId: string) {
  expandedRelation.value = expandedRelation.value === serviceId ? null : serviceId
}

function addRelationEnvPair(rel: CredentialRelation) {
  rel.default_env.push({ key: '', value: '' })
}

function removeRelationEnvPair(rel: CredentialRelation, i: number) {
  rel.default_env.splice(i, 1)
}

function addRelationCommand(rel: CredentialRelation) {
  rel.commands.push({
    phase: 'configure',
    commandText: '',
    workdir: '/workspace',
    env: [],
    description: '',
    order: rel.commands.length,
    _dragId: _dragCounter++,
  })
}

function removeRelationCommand(rel: CredentialRelation, row: CommandRow) {
  const idx = rel.commands.findIndex((c) => c._dragId === row._dragId)
  if (idx !== -1) rel.commands.splice(idx, 1)
}

function onRelationDragStart(e: DragEvent, row: CommandRow) {
  _dragId = row._dragId
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
}

function onRelationDragOver(e: DragEvent) {
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
}

function onRelationDrop(e: DragEvent, rel: CredentialRelation, targetRow: CommandRow) {
  e.preventDefault()
  const fromIndex = rel.commands.findIndex((c) => c._dragId === _dragId)
  const toIndex = rel.commands.findIndex((c) => c._dragId === targetRow._dragId)
  if (fromIndex === -1 || toIndex === -1 || fromIndex === toIndex) return
  const [moved] = rel.commands.splice(fromIndex, 1)
  if (!moved) return
  rel.commands.splice(toIndex, 0, moved)
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

function validate(): string | null {
  if (!name.value.trim()) return 'Name is required'
  const runCmds = commands.value.filter((c) => c.phase === 'run')
  if (runCmds.length === 0) return 'At least one run command is required'
  if (runCmds.length > 1) return 'Only one run command is allowed'
  for (const cmd of commands.value) {
    if (!cmd.commandText.trim()) return 'All commands must have a command string'
  }
  return null
}

// ---------------------------------------------------------------------------
// Submit
// ---------------------------------------------------------------------------

async function submit() {
  const err = validate()
  if (err) {
    error.value = err
    return
  }

  loading.value = true
  error.value = null

  try {
    const payload = {
      name: name.value.trim(),
      description: description.value.trim(),
      default_env: pairsToEnvObj(defaultEnv.value),
      supports_multi_chat: supportsMultiChat.value,
      available_options: optionsToApi(),
      commands: rowsToApiCommands(commands.value),
      required_credential_service_ids: credentialRelations.value.map(
        (r) => r.credential_service_id,
      ),
    }

    let savedAgent: AgentDefinitionFull

    if (props.agent) {
      savedAgent = await patch<AgentDefinitionFull>(
        `/org-agent-definitions/${props.agent.id}/`,
        payload,
      )
    } else {
      savedAgent = await post<AgentDefinitionFull>('/org-agent-definitions/', payload)
    }

    await syncCredentialRelations(savedAgent.id)

    emit('saved', savedAgent)
    emit('update:open', false)
  } catch (e: unknown) {
    error.value = `Failed: ${(e as Error).message}`
  } finally {
    loading.value = false
  }
}

async function syncCredentialRelations(agentId: string) {
  let existing: { id: string; credential_service_id: string }[] = []
  try {
    existing = await get(`/org-agent-definitions/${agentId}/credential-relations/`)
  } catch {
    existing = []
  }

  const existingMap = new Map(existing.map((r) => [r.credential_service_id, r]))
  const desiredIds = new Set(credentialRelations.value.map((r) => r.credential_service_id))

  for (const [svcId, existingRel] of existingMap) {
    if (!desiredIds.has(svcId)) {
      try {
        await del(`/org-agent-definitions/${agentId}/credential-relations/${existingRel.id}/`)
      } catch {
        /* ignore */
      }
    }
  }

  for (const rel of credentialRelations.value) {
    const relPayload = {
      default_env: pairsToEnvObj(rel.default_env),
      commands: rowsToApiCommands(rel.commands),
    }
    const existing_ = existingMap.get(rel.credential_service_id)
    if (existing_) {
      try {
        await patch(
          `/org-agent-definitions/${agentId}/credential-relations/${existing_.id}/`,
          relPayload,
        )
      } catch {
        /* ignore */
      }
    } else {
      try {
        await post(`/org-agent-definitions/${agentId}/credential-relations/`, {
          credential_service_id: rel.credential_service_id,
          ...relPayload,
        })
      } catch {
        /* ignore */
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Close
// ---------------------------------------------------------------------------

function close() {
  if (!loading.value) emit('update:open', false)
}
</script>

<template>
  <UiDialog
    :open="open"
    :title="agent ? `Edit Agent: ${agent.name}` : 'New Agent Definition'"
    description=""
    class="sm:max-w-2xl lg:max-w-5xl"
    @update:open="(v) => !v && close()"
  >
    <template #trigger>
      <span class="hidden" />
    </template>

    <div class="flex flex-col gap-4">
      <!-- Error banner -->
      <div
        v-if="error"
        class="flex items-center gap-2 rounded-[var(--radius-sm)] border border-error/30 bg-error-muted px-3 py-2 text-sm text-error"
      >
        <AlertTriangle :size="14" class="shrink-0" />
        <span class="flex-1">{{ error }}</span>
        <UiButton variant="ghost" size="icon-sm" @click="error = null">
          <X :size="12" />
        </UiButton>
      </div>

      <!-- Tabs -->
      <div class="flex gap-1 border-b border-border -mx-1 px-1">
        <button
          v-for="tab in [
            { key: 'definition', label: 'Definition' },
            { key: 'credentials', label: 'Credentials' },
          ]"
          :key="tab.key"
          type="button"
          class="px-3 py-1.5 text-sm font-medium rounded-t border-b-2 transition-colors"
          :class="
            activeTab === tab.key
              ? 'border-accent text-accent'
              : 'border-transparent text-muted-fg hover:text-fg'
          "
          @click="activeTab = tab.key as 'definition' | 'credentials'"
        >
          {{ tab.label }}
          <span
            v-if="tab.key === 'credentials' && credentialRelations.length > 0"
            class="ml-1.5 text-xs px-1 rounded-full bg-accent/15 text-accent"
          >{{ credentialRelations.length }}</span>
        </button>
      </div>

      <!-- ================================================================ -->
      <!-- TAB: Definition -->
      <!-- ================================================================ -->
      <div v-if="activeTab === 'definition'" class="grid grid-cols-1 lg:grid-cols-[2fr_3fr] gap-6">

        <!-- ── LEFT COLUMN: Settings ── -->
        <div class="space-y-5">

          <!-- Name -->
          <div>
            <label class="block text-xs font-medium text-muted-fg mb-1">Name *</label>
            <UiInput
              v-model="name"
              placeholder="e.g. my-custom-agent"
              :disabled="agent?.is_standard"
            />
          </div>

          <!-- Description -->
          <div>
            <label class="block text-xs font-medium text-muted-fg mb-1">Description</label>
            <UiTextarea
              v-model="description"
              :rows="2"
              placeholder="What does this agent do?"
              class="text-sm resize-none"
            />
          </div>

          <!-- Multi-chat toggle -->
          <div>
            <label class="text-xs font-medium text-muted-fg block mb-2">Options</label>
            <label class="flex items-center gap-3 cursor-pointer">
              <div
                class="relative w-10 h-6 rounded-full transition-colors cursor-pointer shrink-0 border"
                :class="
                  supportsMultiChat
                    ? 'bg-accent border-accent'
                    : 'bg-zinc-400 dark:bg-zinc-600 border-zinc-400 dark:border-zinc-600'
                "
                @click="supportsMultiChat = !supportsMultiChat"
              >
                <div
                  class="absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow-sm transition-transform duration-200"
                  :class="supportsMultiChat ? 'translate-x-4' : 'translate-x-0'"
                />
              </div>
              <span class="text-sm text-fg">Multi-chat support</span>
            </label>
          </div>

          <!-- Default Environment Variables -->
          <div>
            <div class="flex items-center justify-between mb-2">
              <label class="text-xs font-medium text-muted-fg">Default Environment Variables</label>
              <UiButton size="icon-sm" variant="ghost" title="Add variable" @click="addEnvPair">
                <Plus :size="14" />
              </UiButton>
            </div>

            <div v-if="defaultEnv.length === 0" class="text-xs text-muted-fg py-1.5 text-center">
              No environment variables.
            </div>

            <div class="space-y-1.5">
              <div
                v-for="(pair, i) in defaultEnv"
                :key="i"
                class="flex gap-2 items-center"
              >
                <UiInput
                  v-model="pair.key"
                  placeholder="KEY"
                  class="font-mono text-xs flex-1"
                />
                <span class="text-muted-fg text-xs shrink-0">=</span>
                <UiInput
                  v-model="pair.value"
                  placeholder="value"
                  class="font-mono text-xs flex-1"
                />
                <UiButton variant="ghost" size="icon-sm" @click="removeEnvPair(i)">
                  <X :size="12" />
                </UiButton>
              </div>
            </div>
          </div>

          <!-- Available Options -->
          <div>
            <div class="flex items-center justify-between mb-2">
              <div>
                <label class="text-xs font-medium text-muted-fg">Available Options</label>
                <p class="text-xs text-muted-fg/70 mt-0.5">
                  Selectable per-session options. Keys become <code class="font-mono bg-muted/40 px-0.5 rounded text-accent">{key}</code> placeholders in commands.
                </p>
              </div>
              <UiButton size="icon-sm" variant="ghost" title="Add option" @click="addOption">
                <Plus :size="14" />
              </UiButton>
            </div>

            <div v-if="availableOptions.length === 0" class="text-xs text-muted-fg py-1.5 text-center">
              No options configured.
            </div>

            <div class="space-y-2">
              <div
                v-for="(opt, i) in availableOptions"
                :key="opt._id"
                class="rounded-[var(--radius-sm)] border border-border p-2.5 space-y-2"
                style="background: var(--glass-bg-subtle)"
              >
                <div class="flex items-center gap-2">
                  <div class="flex-1 grid grid-cols-2 gap-1.5">
                    <div>
                      <label class="text-xs text-muted-fg">Key</label>
                      <UiInput
                        v-model="opt.key"
                        placeholder="model"
                        class="font-mono text-xs mt-0.5"
                      />
                    </div>
                    <div>
                      <label class="text-xs text-muted-fg">Label</label>
                      <UiInput
                        v-model="opt.label"
                        placeholder="Model"
                        class="text-xs mt-0.5"
                      />
                    </div>
                  </div>
                  <UiButton
                    variant="ghost"
                    size="icon-sm"
                    class="shrink-0 self-end mb-0.5"
                    @click="removeOption(i)"
                  >
                    <X :size="12" />
                  </UiButton>
                </div>
                <div class="grid grid-cols-2 gap-1.5">
                  <div>
                    <label class="text-xs text-muted-fg">Choices <span class="text-muted-fg/60">(comma-separated)</span></label>
                    <UiInput
                      v-model="opt.choicesText"
                      placeholder="gpt-4, claude-3"
                      class="font-mono text-xs mt-0.5"
                    />
                  </div>
                  <div>
                    <label class="text-xs text-muted-fg">Default</label>
                    <UiInput
                      v-model="opt.default"
                      :placeholder="opt.choicesText.split(',')[0]?.trim() || 'default'"
                      class="font-mono text-xs mt-0.5"
                    />
                  </div>
                </div>
              </div>
            </div>
          </div>

        </div>

        <!-- ── RIGHT COLUMN: Commands ── -->
        <div class="space-y-4">
          <p class="text-xs font-medium text-muted-fg">Commands</p>

          <!-- ── Configure Phase ── -->
          <div class="rounded-[var(--radius-md)] border border-border overflow-hidden" style="background: var(--glass-bg-subtle)">
            <div class="flex items-center justify-between px-3 py-2.5 border-b border-border">
              <div class="flex items-center gap-2">
                <Settings2 :size="14" class="text-muted-fg" />
                <span class="text-xs font-medium text-fg">Configure Phase</span>
                <span class="text-xs text-muted-fg">(runs once after workspace setup)</span>
              </div>
              <UiButton size="sm" variant="ghost" @click="addConfigureCommand">
                <Plus :size="12" />
                Add
              </UiButton>
            </div>

            <div class="p-3">
              <div
                v-if="configureCommands.length === 0"
                class="text-xs text-muted-fg py-3 text-center border border-dashed border-border rounded-[var(--radius-sm)]"
              >
                No configure commands. Optional.
              </div>

              <div class="space-y-2">
                <div
                  v-for="row in configureCommands"
                  :key="row._dragId"
                  class="rounded-[var(--radius-sm)] border border-border p-2.5 space-y-2 bg-[var(--glass-bg)]"
                  draggable="true"
                  @dragstart="onDragStart($event, row)"
                  @dragover="onDragOver($event, row)"
                  @drop="onDrop($event, row)"
                >
                  <div class="flex items-start gap-2">
                    <GripVertical :size="14" class="text-muted-fg mt-2 shrink-0 cursor-grab" />
                    <div class="flex-1 space-y-1.5">
                      <UiInput
                        v-model="row.commandText"
                        placeholder="bash -c 'echo hello'"
                        class="font-mono text-xs"
                      />
                      <div v-if="row.commandText" class="flex gap-1 flex-wrap">
                        <span
                          v-for="p in validatePlaceholders(row.commandText).valid"
                          :key="p"
                          class="text-xs px-1.5 py-0.5 rounded bg-accent/15 text-accent font-mono"
                        >{{ p }}</span>
                        <span
                          v-for="p in validatePlaceholders(row.commandText).invalid"
                          :key="p"
                          class="text-xs px-1.5 py-0.5 rounded bg-error/15 text-error font-mono"
                        >{{ p }} ✗</span>
                      </div>
                      <UiInput
                        v-model="row.description"
                        placeholder="Description (optional)"
                        class="text-xs"
                      />
                      <!-- Per-command env -->
                      <div v-if="row.env.length > 0 || true">
                        <div class="flex items-center justify-between mb-1">
                          <label class="text-xs text-muted-fg">Extra Env</label>
                          <UiButton variant="ghost" size="icon-sm" @click="addCommandEnvPair(row)">
                            <Plus :size="10" />
                          </UiButton>
                        </div>
                        <div class="space-y-1">
                          <div
                            v-for="(pair, ei) in row.env"
                            :key="ei"
                            class="flex gap-1.5 items-center"
                          >
                            <UiInput v-model="pair.key" placeholder="KEY" class="font-mono text-xs" />
                            <span class="text-muted-fg text-xs shrink-0">=</span>
                            <UiInput v-model="pair.value" placeholder="val" class="font-mono text-xs" />
                            <UiButton variant="ghost" size="icon-sm" @click="removeCommandEnvPair(row, ei)">
                              <X :size="10" />
                            </UiButton>
                          </div>
                        </div>
                        <div v-if="row.env.length === 0">
                          <UiButton
                            variant="ghost"
                            size="sm"
                            class="text-xs text-muted-fg w-full justify-start"
                            @click="addCommandEnvPair(row)"
                          >
                            <Plus :size="10" />
                            Add env var
                          </UiButton>
                        </div>
                      </div>
                    </div>
                    <UiButton variant="ghost" size="icon-sm" class="shrink-0 mt-0.5" @click="removeCommand(row)">
                      <Trash2 :size="13" />
                    </UiButton>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- ── Run (First Message) Phase ── -->
          <div
            class="rounded-[var(--radius-md)] border overflow-hidden"
            :class="runFirstCommand ? 'border-amber-400/30' : 'border-border border-dashed'"
            style="background: var(--glass-bg-subtle)"
          >
            <div
              class="flex items-center justify-between px-3 py-2.5"
              :class="runFirstCommand ? 'border-b border-amber-400/20' : ''"
            >
              <div class="flex items-center gap-2">
                <Zap :size="14" :class="runFirstCommand ? 'text-amber-400' : 'text-muted-fg'" />
                <span class="text-xs font-medium text-fg">Run Phase (First Message)</span>
                <span class="text-xs text-muted-fg">(optional — runs on first chat message)</span>
              </div>
              <UiButton
                v-if="!runFirstCommand"
                size="sm"
                variant="ghost"
                class="text-xs"
                @click="enableRunFirst"
              >
                <Plus :size="12" />
                Enable
              </UiButton>
              <UiButton
                v-else
                size="sm"
                variant="ghost"
                class="text-xs text-muted-fg"
                @click="disableRunFirst"
              >
                <X :size="12" />
                Disable
              </UiButton>
            </div>

            <div v-if="runFirstCommand" class="p-3">
              <div
                class="rounded-[var(--radius-sm)] border border-amber-400/20 p-2.5 space-y-1.5 bg-[var(--glass-bg)]"
              >
                <div class="flex items-start gap-2">
                  <Zap :size="14" class="text-amber-400 mt-2 shrink-0" />
                  <div class="flex-1 space-y-1.5">
                    <UiInput
                      v-model="runFirstCommand.commandText"
                      placeholder="my-agent --init {prompt}"
                      class="font-mono text-xs"
                    />
                    <div v-if="runFirstCommand.commandText" class="flex gap-1 flex-wrap">
                      <span
                        v-for="p in validatePlaceholders(runFirstCommand.commandText).valid"
                        :key="p"
                        class="text-xs px-1.5 py-0.5 rounded bg-accent/15 text-accent font-mono"
                      >{{ p }}</span>
                      <span
                        v-for="p in validatePlaceholders(runFirstCommand.commandText).invalid"
                        :key="p"
                        class="text-xs px-1.5 py-0.5 rounded bg-error/15 text-error font-mono"
                      >{{ p }} ✗</span>
                    </div>
                    <UiInput
                      v-model="runFirstCommand.description"
                      placeholder="Description (optional)"
                      class="text-xs"
                    />
                    <!-- Per-command env -->
                    <div>
                      <div class="flex items-center justify-between mb-1">
                        <label class="text-xs text-muted-fg">Extra Env</label>
                        <UiButton variant="ghost" size="icon-sm" @click="addCommandEnvPair(runFirstCommand)">
                          <Plus :size="10" />
                        </UiButton>
                      </div>
                      <div class="space-y-1">
                        <div
                          v-for="(pair, ei) in runFirstCommand.env"
                          :key="ei"
                          class="flex gap-1.5 items-center"
                        >
                          <UiInput v-model="pair.key" placeholder="KEY" class="font-mono text-xs" />
                          <span class="text-muted-fg text-xs shrink-0">=</span>
                          <UiInput v-model="pair.value" placeholder="val" class="font-mono text-xs" />
                          <UiButton variant="ghost" size="icon-sm" @click="removeCommandEnvPair(runFirstCommand, ei)">
                            <X :size="10" />
                          </UiButton>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- ── Run Phase ── -->
          <div
            class="rounded-[var(--radius-md)] border overflow-hidden"
            :class="runCommands.length === 0 ? 'border-error/30' : 'border-accent/20'"
            style="background: var(--glass-bg-subtle)"
          >
            <div
              class="flex items-center justify-between px-3 py-2.5 border-b"
              :class="runCommands.length === 0 ? 'border-error/20' : 'border-accent/10'"
            >
              <div class="flex items-center gap-2">
                <Bot :size="14" class="text-accent" />
                <span class="text-xs font-medium text-fg">Run Phase</span>
                <span class="text-xs text-muted-fg">(executed for each prompt — exactly 1)</span>
              </div>
              <UiButton
                size="sm"
                variant="ghost"
                :disabled="runCommands.length >= 1"
                @click="addRunCommand"
              >
                <Plus :size="12" />
                Add
              </UiButton>
            </div>

            <div class="p-3">
              <div
                v-if="runCommands.length === 0"
                class="text-xs text-error py-3 text-center border border-dashed border-error/30 rounded-[var(--radius-sm)]"
              >
                Required: add exactly one run command.
              </div>

              <div class="space-y-2">
                <div
                  v-for="row in runCommands"
                  :key="row._dragId"
                  class="rounded-[var(--radius-sm)] border border-accent/20 p-2.5 space-y-1.5 bg-[var(--glass-bg)]"
                >
                  <div class="flex items-start gap-2">
                    <Bot :size="14" class="text-accent mt-2 shrink-0" />
                    <div class="flex-1 space-y-1.5">
                      <UiInput
                        v-model="row.commandText"
                        placeholder="my-agent --prompt {prompt}"
                        class="font-mono text-xs"
                      />
                      <div v-if="row.commandText" class="flex gap-1 flex-wrap">
                        <span
                          v-for="p in validatePlaceholders(row.commandText).valid"
                          :key="p"
                          class="text-xs px-1.5 py-0.5 rounded bg-accent/15 text-accent font-mono"
                        >{{ p }}</span>
                        <span
                          v-for="p in validatePlaceholders(row.commandText).invalid"
                          :key="p"
                          class="text-xs px-1.5 py-0.5 rounded bg-error/15 text-error font-mono"
                        >{{ p }} ✗</span>
                      </div>
                      <UiInput
                        v-model="row.description"
                        placeholder="Description (optional)"
                        class="text-xs"
                      />
                      <!-- Per-command env -->
                      <div>
                        <div class="flex items-center justify-between mb-1">
                          <label class="text-xs text-muted-fg">Extra Env</label>
                          <UiButton variant="ghost" size="icon-sm" @click="addCommandEnvPair(row)">
                            <Plus :size="10" />
                          </UiButton>
                        </div>
                        <div class="space-y-1">
                          <div
                            v-for="(pair, ei) in row.env"
                            :key="ei"
                            class="flex gap-1.5 items-center"
                          >
                            <UiInput v-model="pair.key" placeholder="KEY" class="font-mono text-xs" />
                            <span class="text-muted-fg text-xs shrink-0">=</span>
                            <UiInput v-model="pair.value" placeholder="val" class="font-mono text-xs" />
                            <UiButton variant="ghost" size="icon-sm" @click="removeCommandEnvPair(row, ei)">
                              <X :size="10" />
                            </UiButton>
                          </div>
                        </div>
                      </div>
                    </div>
                    <UiButton variant="ghost" size="icon-sm" class="shrink-0 mt-0.5" @click="removeCommand(row)">
                      <Trash2 :size="13" />
                    </UiButton>
                  </div>
                </div>
              </div>
            </div>
          </div>

        </div>
      </div>

      <!-- ================================================================ -->
      <!-- TAB: Credentials -->
      <!-- ================================================================ -->
      <div v-else-if="activeTab === 'credentials'" class="space-y-4">
        <p class="text-xs text-muted-fg">
          Select which credential services this agent requires. You can optionally configure
          per-service default environment variables and commands.
        </p>

        <!-- Available services to add -->
        <div>
          <p class="text-xs font-medium text-muted-fg mb-2">Available Credential Services</p>
          <div class="flex flex-wrap gap-2">
            <button
              v-for="svc in credentialServices"
              :key="svc.id"
              type="button"
              class="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs border transition-colors"
              :class="
                linkedCredentialServiceIds.includes(svc.id)
                  ? 'border-accent/40 bg-accent/10 text-accent cursor-default'
                  : 'border-border text-muted-fg hover:border-accent/40 hover:text-fg cursor-pointer'
              "
              :disabled="linkedCredentialServiceIds.includes(svc.id)"
              @click="addCredentialRelation(svc)"
            >
              <Check v-if="linkedCredentialServiceIds.includes(svc.id)" :size="11" />
              <Plus v-else :size="11" />
              {{ svc.name }}
            </button>
            <span v-if="credentialServices.length === 0" class="text-xs text-muted-fg">
              No credential services available.
            </span>
          </div>
        </div>

        <!-- Linked relations -->
        <div class="space-y-2">
          <div
            v-for="rel in credentialRelations"
            :key="rel.credential_service_id"
            class="rounded-[var(--radius-md)] border border-border overflow-hidden"
            style="background: var(--glass-bg)"
          >
            <!-- Header -->
            <div class="flex items-center gap-2 px-3 py-2.5">
              <button
                type="button"
                class="flex items-center gap-1 text-muted-fg hover:text-fg"
                @click="toggleRelationExpand(rel.credential_service_id)"
              >
                <ChevronDown v-if="expandedRelation !== rel.credential_service_id" :size="14" />
                <ChevronUp v-else :size="14" />
              </button>
              <Key :size="14" class="text-muted-fg shrink-0" />
              <span class="text-sm font-medium text-fg flex-1">
                {{ rel.credential_service_name || rel.credential_service_id }}
              </span>
              <div class="flex items-center gap-1.5">
                <span
                  v-if="rel.default_env.length > 0 || rel.commands.length > 0"
                  class="text-xs text-muted-fg"
                >
                  <span v-if="rel.default_env.length > 0">{{ rel.default_env.length }} env</span>
                  <span v-if="rel.default_env.length > 0 && rel.commands.length > 0"> · </span>
                  <span v-if="rel.commands.length > 0">{{ rel.commands.length }} cmd</span>
                </span>
                <span v-else class="text-xs text-muted-fg italic">required only</span>
                <UiButton
                  variant="ghost"
                  size="icon-sm"
                  title="Remove"
                  @click="removeCredentialRelation(rel.credential_service_id)"
                >
                  <X :size="12" />
                </UiButton>
              </div>
            </div>

            <!-- Expanded detail -->
            <div
              v-if="expandedRelation === rel.credential_service_id"
              class="border-t border-border px-3 py-3 space-y-3"
            >
              <!-- Default env for this relation -->
              <div>
                <div class="flex items-center justify-between mb-1.5">
                  <label class="text-xs font-medium text-muted-fg">Default Env (for this service)</label>
                  <UiButton variant="ghost" size="icon-sm" @click="addRelationEnvPair(rel)">
                    <Plus :size="12" />
                  </UiButton>
                </div>
                <div v-if="rel.default_env.length === 0" class="text-xs text-muted-fg italic">
                  No extra env variables. Click + to add one.
                </div>
                <div class="space-y-1.5">
                  <div
                    v-for="(pair, i) in rel.default_env"
                    :key="i"
                    class="flex gap-2 items-center"
                  >
                    <UiInput v-model="pair.key" placeholder="KEY" class="font-mono text-xs flex-1" />
                    <span class="text-muted-fg text-xs shrink-0">=</span>
                    <UiInput v-model="pair.value" placeholder="value" class="font-mono text-xs flex-1" />
                    <UiButton variant="ghost" size="icon-sm" @click="removeRelationEnvPair(rel, i)">
                      <X :size="12" />
                    </UiButton>
                  </div>
                </div>
              </div>

              <!-- Commands for this relation -->
              <div>
                <div class="flex items-center justify-between mb-1.5">
                  <label class="text-xs font-medium text-muted-fg">Commands (run before agent commands)</label>
                  <UiButton size="sm" variant="ghost" @click="addRelationCommand(rel)">
                    <Plus :size="12" />
                    Add
                  </UiButton>
                </div>
                <div v-if="rel.commands.length === 0" class="text-xs text-muted-fg italic">
                  No commands. Typically you only need to add the credential service above.
                </div>
                <div class="space-y-2">
                  <div
                    v-for="row in rel.commands"
                    :key="row._dragId"
                    class="rounded-[var(--radius-sm)] border border-border p-2.5 space-y-2"
                    style="background: var(--glass-bg-subtle)"
                    draggable="true"
                    @dragstart="onRelationDragStart($event, row)"
                    @dragover="onRelationDragOver"
                    @drop="onRelationDrop($event, rel, row)"
                  >
                    <div class="flex items-start gap-2">
                      <GripVertical :size="13" class="text-muted-fg mt-2 cursor-grab shrink-0" />
                      <div class="flex-1 space-y-2">
                        <!-- Phase selector -->
                        <div class="flex gap-2 items-center">
                          <select
                            v-model="row.phase"
                            class="h-7 px-2 rounded-[var(--radius-sm)] border border-[var(--input-border)] bg-[var(--input-bg)] text-xs text-fg"
                          >
                            <option value="configure">configure</option>
                            <option value="run">run</option>
                          </select>
                          <UiInput
                            v-model="row.commandText"
                            placeholder="bash -c '...'"
                            class="font-mono text-xs flex-1"
                          />
                        </div>
                        <div v-if="row.commandText" class="flex gap-1 flex-wrap">
                          <span
                            v-for="p in validatePlaceholders(row.commandText).valid"
                            :key="p"
                            class="text-xs px-1.5 py-0.5 rounded bg-accent/15 text-accent font-mono"
                          >{{ p }}</span>
                          <span
                            v-for="p in validatePlaceholders(row.commandText).invalid"
                            :key="p"
                            class="text-xs px-1.5 py-0.5 rounded bg-error/15 text-error font-mono"
                          >{{ p }} ✗</span>
                        </div>
                        <UiInput
                          v-model="row.description"
                          placeholder="Description (optional)"
                          class="text-xs"
                        />
                      </div>
                      <UiButton variant="ghost" size="icon-sm" @click="removeRelationCommand(rel, row)">
                        <Trash2 :size="12" />
                      </UiButton>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div v-if="credentialRelations.length === 0" class="text-center py-8 text-muted-fg text-sm">
            No credential services linked. Select one above to get started.
          </div>
        </div>
      </div>

      <!-- ── Action buttons ── -->
      <div class="flex justify-end gap-2 pt-2 border-t border-border">
        <UiButton variant="outline" type="button" :disabled="loading" @click="close">
          Cancel
        </UiButton>
        <UiButton type="button" :disabled="loading || !name.trim()" @click="submit">
          <UiSpinner v-if="loading" :size="12" />
          <Check v-else :size="12" />
          {{ agent ? 'Save Changes' : 'Create Agent' }}
        </UiButton>
      </div>
    </div>
  </UiDialog>
</template>
