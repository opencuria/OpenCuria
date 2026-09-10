<!--
  AgentConfigTab — per-agent model/effort configuration.

  Primary agents (build, plan) always use a fixed model; subagents
  (general, explore, computeruse) may inherit the parent run model or
  use a fixed model, or map the parent effort via a strategy.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import ProviderModelCombobox from './ProviderModelCombobox.vue'
import SettingsSection from './SettingsSection.vue'
import {
  CONFIGURABLE_AGENTS,
  EFFORT_STRATEGIES,
  PRIMARY_AGENTS,
  SUBAGENT_IDS,
  agentDisplayName,
  resolvePreviewEffort,
  type AgentConfig,
  type AgentConfigId,
  type EffortStrategy,
} from '@/lib/harnessAgents'
import { invalidateAgentConfigs, loadAgentConfigsCached } from '@/lib/agentConfigs'
import { formatEffort, resolveCatalogModel, type ProviderModel } from '@/lib/harnessModels'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import { saveAgentConfigs, type AgentConfigIn } from '@/services/harness.api'
import { useNotificationStore } from '@/stores/notifications'

const notifications = useNotificationStore()

const loading = ref(true)
const saving = ref(false)
const error = ref<string | null>(null)
const catalog = ref<ProviderModel[]>([])
const loaded = ref<AgentConfig[]>([])

interface AgentState {
  model: string
  effort: string
  inherit: boolean
  strategy: EffortStrategy
  description: string
}

const state = ref<Record<AgentConfigId, AgentState>>(
  Object.fromEntries(CONFIGURABLE_AGENTS.map((a) => [a, defaultState()])) as Record<AgentConfigId, AgentState>,
)

function defaultState(): AgentState {
  return { model: '', effort: '', inherit: false, strategy: 'inherit', description: '' }
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
      inherit: cfg?.inherit_model ?? false,
      strategy: (cfg?.effort_strategy as EffortStrategy | undefined) ?? 'inherit',
      description: cfg?.description ?? '',
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
    const [configs, models] = await Promise.all([
      loadAgentConfigsCached(),
      loadProviderModelsCached().catch(() => [] as ProviderModel[]),
    ])
    catalog.value = models
    applyConfigs(configs)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to load agent settings'
  } finally {
    loading.value = false
  }
}

/** Preview effort list: resolve against the build agent's configured model. */
const buildModelEfforts = computed<string[]>(() => {
  const buildModel = state.value['build']?.model ?? ''
  return resolveCatalogModel(catalog.value, buildModel)?.reasoning_efforts ?? []
})

function strategyHint(agent: AgentConfigId): string {
  const s = agentState(agent)
  if (s.strategy === 'inherit') return 'Uses the parent run effort'
  const preview = resolvePreviewEffort(buildModelEfforts.value, s.strategy)
  const label = EFFORT_STRATEGIES.find((o) => o.value === s.strategy)?.label ?? s.strategy
  return preview ? `${label} → ${formatEffort(preview)}` : `${label} effort of the parent model`
}

const dirty = computed(() => {
  const byAgent = new Map(loaded.value.map((c) => [c.agent, c]))
  for (const agent of CONFIGURABLE_AGENTS) {
    const s = agentState(agent)
    const c = byAgent.get(agent)
    const isPrimary = (PRIMARY_AGENTS as string[]).includes(agent)
    const inherit = isPrimary ? false : s.inherit
    const strategy: string = isPrimary ? 'fixed' : inherit ? s.strategy : 'fixed'
    const model = inherit ? '' : s.model.trim()
    const effort = inherit ? '' : s.effort.trim()
    if (model !== (c?.model ?? '')) return true
    if (effort !== (c?.effort ?? '')) return true
    if (inherit !== (c?.inherit_model ?? false)) return true
    if (strategy !== (c?.effort_strategy ?? '')) return true
  }
  return false
})

const primaryMissing = computed(() =>
  (PRIMARY_AGENTS as string[]).some((a) => !agentState(a as AgentConfigId).model.trim()),
)

async function handleSave(): Promise<void> {
  if (saving.value || !dirty.value || primaryMissing.value) return
  saving.value = true
  try {
    const payload: AgentConfigIn[] = CONFIGURABLE_AGENTS.map((agent) => {
      const s = agentState(agent)
      const isPrimary = (PRIMARY_AGENTS as string[]).includes(agent)
      if (isPrimary) {
        return { agent, model: s.model.trim(), effort: s.effort.trim(), inherit_model: false, effort_strategy: 'fixed' }
      }
      if (s.inherit) {
        return { agent, model: '', effort: '', inherit_model: true, effort_strategy: s.strategy }
      }
      return { agent, model: s.model.trim(), effort: s.effort.trim(), inherit_model: false, effort_strategy: 'fixed' }
    })
    const saved = await saveAgentConfigs(payload)
    applyConfigs(saved)
    invalidateAgentConfigs()
    notifications.success('Agent models saved')
  } catch (e: unknown) {
    notifications.error('Failed to save agent models', e instanceof Error ? e.message : undefined)
  } finally {
    saving.value = false
  }
}

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
      <SettingsSection title="Primary agents" description="Models for new build and plan runs. A model is required.">
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
              <p class="text-sm text-muted-foreground">{{ agentState(agent).description }}</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ProviderModelCombobox
                :input-id="`agent-model-${agent}`"
                v-model="state[agent].model"
                :effort="state[agent].effort"
                :models="catalog"
                empty-hint="Connect a provider under Provider & Models to browse models."
                @update:effort="state[agent].effort = $event"
              />
            </div>
          </div>
        </div>
        <p v-if="primaryMissing" class="text-xs text-muted-foreground">Select a model</p>
      </SettingsSection>

      <SettingsSection title="Subagents" description="Helper agents spawned during a run. Inherit the parent run model or pick a custom model.">
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            v-for="agent in SUBAGENT_IDS"
            :key="agent"
            :data-testid="`agent-row-${agent}`"
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-medium text-foreground">{{ agentDisplayName(agent) }}</span>
                <span class="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">Subagent</span>
              </div>
              <p class="text-sm text-muted-foreground">{{ agentState(agent).description }}</p>
              <div class="flex items-center gap-4 pt-1" role="radiogroup" :aria-label="`${agentDisplayName(agent)} mode`">
                <label class="flex items-center gap-1.5 text-sm">
                  <input
                    type="radio"
                    :name="`agent-mode-${agent}`"
                    :checked="!state[agent].inherit"
                    :data-testid="`agent-mode-custom-${agent}`"
                    @change="state[agent].inherit = false"
                  />
                  Custom
                </label>
                <label class="flex items-center gap-1.5 text-sm">
                  <input
                    type="radio"
                    :name="`agent-mode-${agent}`"
                    :checked="state[agent].inherit"
                    :data-testid="`agent-mode-inherit-${agent}`"
                    @change="state[agent].inherit = true"
                  />
                  Inherit from parent run
                </label>
              </div>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <div :class="state[agent].inherit ? 'pointer-events-none opacity-50' : undefined" :aria-disabled="state[agent].inherit">
                <ProviderModelCombobox
                  :input-id="`agent-model-${agent}`"
                  v-model="state[agent].model"
                  :effort="state[agent].effort"
                  :models="catalog"
                  empty-hint="Connect a provider under Provider & Models to browse models."
                  @update:effort="state[agent].effort = $event"
                />
              </div>
              <div v-if="state[agent].inherit" class="mt-2 space-y-1">
                <select
                  :data-testid="`agent-strategy-${agent}`"
                  :value="state[agent].strategy"
                  class="h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
                  @change="state[agent].strategy = (($event.target as HTMLSelectElement).value as EffortStrategy)"
                >
                  <option
                    v-for="opt in EFFORT_STRATEGIES.filter((o) => o.value !== 'fixed')"
                    :key="opt.value"
                    :value="opt.value"
                  >
                    {{ opt.label }}
                  </option>
                </select>
                <p class="text-xs text-muted-foreground" :data-testid="`agent-strategy-hint-${agent}`">
                  {{ strategyHint(agent) }}
                </p>
              </div>
            </div>
          </div>
        </div>
      </SettingsSection>

      <div class="flex items-center justify-between gap-3">
        <p class="text-xs text-muted-foreground" data-testid="agent-configs-status">
          {{ dirty ? 'Unsaved changes' : 'All changes saved' }}
        </p>
        <Button
          size="sm"
          :disabled="saving || !dirty || primaryMissing"
          data-testid="save-agent-configs"
          @click="handleSave"
        >
          <LoadingSpinner v-if="saving" :size="12" />
          <span v-else>Save Agents</span>
        </Button>
      </div>
    </template>
  </div>
</template>
