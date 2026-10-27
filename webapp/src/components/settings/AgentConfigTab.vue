<!--
  AgentConfigTab — per-agent model/effort configuration.

  Primary agents (build, plan) always use a fixed model; subagents
  (general, explore, computeruse) may inherit the parent run model or
  use a fixed model, or map the parent effort via a strategy.
  The Computer Use execution limits below reuse the Computer Use subagent
  row as their main model (`ComputerUseConfigPanel`).
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import ComputerUseConfigPanel from './ComputerUseConfigPanel.vue'
import ModelPicker from '@/components/common/ModelPicker.vue'
import SettingsSection from './SettingsSection.vue'
import {
  CONFIGURABLE_AGENTS,
  PRIMARY_AGENTS,
  SUBAGENT_IDS,
  agentDisplayName,
  type AgentConfig,
  type AgentConfigId,
  type EffortStrategy,
} from '@/lib/harnessAgents'
import { invalidateAgentConfigs, loadAgentConfigsCached } from '@/lib/agentConfigs'
import type { ProviderModel } from '@/lib/harnessModels'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import {
  getSubagentConfig,
  saveSubagentConfig,
  saveAgentConfigs,
  type AgentConfigIn,
} from '@/services/harness.api'
import { useAutosave } from '@/composables/useAutosave'
import AutosaveStatus from './AutosaveStatus.vue'

const loading = ref(true)
const error = ref<string | null>(null)
const catalog = ref<ProviderModel[]>([])
const loaded = ref<AgentConfig[]>([])
const loadedMaxDepth = ref(2)
const maxDepth = ref<string | number>('2')

const parsedMaxDepth = computed(() => {
  const raw = String(maxDepth.value).trim()
  const value = Number(raw)
  return /^\d+$/.test(raw) && Number.isSafeInteger(value) && value >= 1 && value <= 2147483647
    ? value
    : null
})
const depthError = computed(() =>
  parsedMaxDepth.value === null ? 'Enter an integer 1–2147483647.' : '',
)
const depthDirty = computed(() => parsedMaxDepth.value !== loadedMaxDepth.value)

interface AgentState {
  model: string
  effort: string
  inherit: boolean
  strategy: EffortStrategy
}

const state = ref<Record<AgentConfigId, AgentState>>(
  Object.fromEntries(CONFIGURABLE_AGENTS.map((a) => [a, defaultState()])) as Record<
    AgentConfigId,
    AgentState
  >,
)

function defaultState(): AgentState {
  return { model: '', effort: '', inherit: false, strategy: 'inherit' }
}

function applyConfigs(next: AgentConfig[]): void {
  loaded.value = next
  const byAgent = new Map(next.map((c) => [c.agent, c]))
  const out = {} as Record<AgentConfigId, AgentState>
  for (const agent of CONFIGURABLE_AGENTS) {
    const cfg = byAgent.get(agent)
    out[agent] = {
      model: cfg?.model ?? '',
      effort: cfg?.effort ?? '',
      inherit: cfg?.inherit_model ?? SUBAGENT_IDS.includes(agent),
      strategy:
        cfg?.effort_strategy && cfg.effort_strategy !== 'fixed' ? cfg.effort_strategy : 'inherit',
    }
  }
  state.value = out
}

function agentState(agent: AgentConfigId): AgentState {
  return state.value[agent]
}

async function loadState(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [configs, subagentConfig, models] = await Promise.all([
      loadAgentConfigsCached(),
      getSubagentConfig(),
      loadProviderModelsCached().catch(() => [] as ProviderModel[]),
    ])
    catalog.value = models
    applyConfigs(configs)
    loadedMaxDepth.value = subagentConfig.max_depth
    maxDepth.value = String(subagentConfig.max_depth)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to load agent settings'
  } finally {
    loading.value = false
  }
}

const agentHints: Record<AgentConfigId, string> = {
  build: 'Write and run code.',
  plan: 'Plan before making changes.',
  general: 'Delegated tasks.',
  explore: 'Read-only research.',
  computeruse: 'Desktop automation.',
}
const inheritedEfforts = [
  { value: 'inherit', label: 'Inherit' },
  { value: 'lowest', label: 'Lowest' },
  { value: 'medium', label: 'Medium' },
  { value: 'highest', label: 'Highest' },
]

function setSubagentModel(agent: AgentConfigId, model: string): void {
  const current = agentState(agent)
  current.inherit = !model.trim()
  current.model = model
  if (current.inherit) current.effort = ''
}

function setSubagentEffort(agent: AgentConfigId, effort: string): void {
  const current = agentState(agent)
  if (current.inherit) {
    current.strategy = (effort || 'inherit') as EffortStrategy
  } else {
    current.effort = effort
  }
}

const changedConfigs = computed<AgentConfigIn[]>(() => {
  const byAgent = new Map(loaded.value.map((config) => [config.agent, config]))
  return CONFIGURABLE_AGENTS.map((agent): AgentConfigIn => {
    const current = agentState(agent)
    const inherit = !PRIMARY_AGENTS.includes(agent) && current.inherit
    return {
      agent,
      model: inherit ? '' : current.model.trim(),
      effort: inherit ? '' : current.effort.trim(),
      inherit_model: inherit,
      effort_strategy: inherit ? current.strategy : 'fixed',
    }
  }).filter((next) => {
    const previous = byAgent.get(next.agent)
    const inherited = previous?.inherit_model ?? SUBAGENT_IDS.includes(next.agent as AgentConfigId)
    return (
      next.model !== (previous?.model ?? '') ||
      next.effort !== (previous?.effort ?? '') ||
      next.inherit_model !== inherited ||
      next.effort_strategy !== (previous?.effort_strategy ?? (inherited ? 'inherit' : 'fixed'))
    )
  })
})

const primaryMissing = computed(() =>
  (PRIMARY_AGENTS as string[]).some((a) => !agentState(a as AgentConfigId).model.trim()),
)

const modelsAutosave = useAutosave({
  source: () => state.value,
  dirty: () => changedConfigs.value.length > 0,
  enabled: () => !loading.value && !error.value,
  valid: () =>
    changedConfigs.value.every(
      (config) => !PRIMARY_AGENTS.includes(config.agent as AgentConfigId) || !!config.model,
    ),
  errorMessage: 'Failed to save agent models',
  save: async (isCurrent) => {
    const saved = await saveAgentConfigs(changedConfigs.value)
    if (isCurrent()) {
      loaded.value = saved
      invalidateAgentConfigs()
    }
  },
})

const depthAutosave = useAutosave({
  source: () => maxDepth.value,
  dirty: () => depthDirty.value,
  enabled: () => !loading.value && !error.value,
  valid: () => !depthError.value,
  errorMessage: 'Failed to save agent settings',
  save: async (isCurrent) => {
    const saved = await saveSubagentConfig({ max_depth: parsedMaxDepth.value! })
    if (isCurrent()) loadedMaxDepth.value = saved.max_depth
  },
})

onMounted(() => {
  void loadState()
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
      <SettingsSection title="Primary agents" description="Defaults for new runs.">
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            v-for="agent in PRIMARY_AGENTS"
            :key="agent"
            :data-testid="`agent-row-${agent}`"
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label :for="`agent-model-${agent}`" class="block text-sm font-medium">
                {{ agentDisplayName(agent) }}
              </Label>
              <p class="text-sm text-muted-foreground">{{ agentHints[agent] }}</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ModelPicker
                :input-id="`agent-model-${agent}`"
                v-model:model="state[agent].model"
                v-model:effort="state[agent].effort"
                :models="catalog"
                variant="field"
                manual-fallback
                effort-fallback="model-default"
                allow-effort-default
                default-effort-label="Provider default"
              />
            </div>
          </div>
        </div>
        <p v-if="primaryMissing" class="text-xs text-muted-foreground">Select a model</p>
      </SettingsSection>

      <SettingsSection title="Subagents" description="Inherit from the parent or choose a model.">
        <div
          class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
          data-testid="subagent-settings-group"
        >
          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
            data-testid="subagent-depth-row"
          >
            <div class="min-w-0 space-y-1">
              <Label for="subagent-max-depth" class="block text-sm font-medium">
                Maximum nesting depth
              </Label>
              <p id="subagent-depth-hint" class="text-sm text-muted-foreground">
                Main agent is depth 0.
              </p>
            </div>
            <div class="w-full shrink-0 space-y-1 sm:w-80">
              <Input
                id="subagent-max-depth"
                v-model="maxDepth"
                type="number"
                min="1"
                max="2147483647"
                step="1"
                data-testid="subagent-max-depth"
                :aria-invalid="!!depthError"
                :aria-describedby="
                  depthError
                    ? 'subagent-depth-hint subagent-depth-scope subagent-depth-error'
                    : 'subagent-depth-hint subagent-depth-scope'
                "
              />
              <AutosaveStatus :state="depthAutosave" data-testid="subagent-depth-status" />
              <p id="subagent-depth-scope" class="text-xs text-muted-foreground">
                Default: 2 · New runs
              </p>
              <p
                v-if="depthError"
                id="subagent-depth-error"
                class="text-xs text-destructive"
                data-testid="subagent-depth-error"
              >
                {{ depthError }}
              </p>
            </div>
          </div>
          <div
            v-for="agent in SUBAGENT_IDS"
            :key="agent"
            :data-testid="`agent-row-${agent}`"
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label :for="`agent-model-${agent}`" class="block text-sm font-medium">
                {{ agentDisplayName(agent) }}
              </Label>
              <p class="text-sm text-muted-foreground">{{ agentHints[agent] }}</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ModelPicker
                :input-id="`agent-model-${agent}`"
                :model="state[agent].inherit ? '' : state[agent].model"
                :effort="state[agent].inherit ? state[agent].strategy : state[agent].effort"
                :models="catalog"
                :inherited-effort-options="inheritedEfforts"
                :default-effort-value="state[agent].strategy"
                variant="field"
                allow-default
                default-model-label="Inherit"
                default-option-label="Inherit"
                default-effort-label="Provider default"
                manual-fallback
                effort-fallback="model-default"
                @update:model="setSubagentModel(agent, $event)"
                @update:effort="setSubagentEffort(agent, $event)"
              />
            </div>
          </div>
        </div>
      </SettingsSection>

      <AutosaveStatus :state="modelsAutosave" data-testid="agent-configs-status" />

      <ComputerUseConfigPanel />
    </template>
  </div>
</template>
