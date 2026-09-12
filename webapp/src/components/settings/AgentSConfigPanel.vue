<!--
  AgentSConfigPanel — org-wide Agent-S harness parameters.

  The Agent-S main model/effort stays in AgentConfigTab (the Computer Use
  subagent row above); this panel only manages harness behavior values
  plus the separate grounding model (`GET/PUT /agent-s-config/`).
  An empty grounding model falls back to the run's main model; an empty
  temperature uses the provider default. Saves send only changed fields
  (the backend merges partial payloads over stored values or defaults).
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import ProviderModelCombobox from './ProviderModelCombobox.vue'
import SettingsSection from './SettingsSection.vue'
import { loadProviderModelsCached } from '@/lib/providerCatalog'
import type { ProviderModel } from '@/lib/harnessModels'
import {
  getAgentSConfig,
  saveAgentSConfig,
  type AgentSConfig,
  type AgentSConfigIn,
} from '@/services/harness.api'
import { useNotificationStore } from '@/stores/notifications'

const notifications = useNotificationStore()

/** Client-side bounds mirror `AgentSConfigService._validate` (backend). */
const DIMENSION_MIN = 1
const DIMENSION_MAX = 7680
const STEPS_MIN = 1
const TEMPERATURE_MIN = 0
const TEMPERATURE_MAX = 2
const DELAY_MIN = 0
const DELAY_MAX = 600

const loading = ref(true)
const saving = ref(false)
const error = ref<string | null>(null)
const loaded = ref<AgentSConfig | null>(null)
const catalog = ref<ProviderModel[]>([])
const fieldErrors = ref<Record<string, string>>({})

const groundingModel = ref('')
const groundingWidth = ref('')
const groundingHeight = ref('')
const temperature = ref('')
const maxSteps = ref('')
const maxTrajectoryLength = ref('')
const screenshotMaxDimension = ref('')
const preDelay = ref('')
const postDelay = ref('')
const waitDelay = ref('')
const enableReflection = ref(true)
const enableCodeAgent = ref(true)

function applyConfig(next: AgentSConfig): void {
  loaded.value = next
  groundingModel.value = next.grounding_model || ''
  groundingWidth.value = String(next.grounding_width)
  groundingHeight.value = String(next.grounding_height)
  temperature.value = next.model_temperature == null ? '' : String(next.model_temperature)
  maxSteps.value = String(next.max_steps)
  maxTrajectoryLength.value = String(next.max_trajectory_length)
  screenshotMaxDimension.value = String(next.screenshot_max_dimension)
  preDelay.value = String(next.action_pre_delay)
  postDelay.value = String(next.action_post_delay)
  waitDelay.value = String(next.wait_delay)
  enableReflection.value = next.enable_reflection
  enableCodeAgent.value = next.enable_code_agent
  fieldErrors.value = {}
}

async function loadState(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [config, models] = await Promise.all([
      getAgentSConfig(),
      loadProviderModelsCached().catch(() => [] as ProviderModel[]),
    ])
    catalog.value = models
    applyConfig(config)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to load Agent-S settings'
  } finally {
    loading.value = false
  }
}

/** Scroll to the Computer Use row (owns the Agent-S main model). */
function gotoComputerUse(): void {
  document
    .querySelector('[data-testid="agent-row-computeruse"]')
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function toText(value: unknown): string {
  return typeof value === 'string' ? value : String(value ?? '')
}

function parseIntField(raw: unknown, min: number, max?: number): number | null {
  const trimmed = toText(raw).trim()
  if (!/^\d+$/.test(trimmed)) return null
  const value = Number(trimmed)
  if (!Number.isSafeInteger(value) || value < min) return null
  if (max !== undefined && value > max) return null
  return value
}

function parseFloatField(raw: unknown, min: number, max: number): number | null {
  const trimmed = toText(raw).trim()
  if (!trimmed) return null
  const value = Number(trimmed)
  if (!Number.isFinite(value) || value < min || value > max) return null
  return value
}

interface ValidatedState {
  payload: AgentSConfigIn
  errors: Record<string, string>
}

/** Validate all fields; the payload holds only values changed vs loaded. */
function validate(): ValidatedState {
  const errors: Record<string, string> = {}
  const payload: AgentSConfigIn = {}
  const base = loaded.value
  if (!base) return { payload, errors }

  const grounding = toText(groundingModel.value).trim()
  if (grounding !== base.grounding_model) payload.grounding_model = grounding

  const width = parseIntField(groundingWidth.value, DIMENSION_MIN, DIMENSION_MAX)
  if (width === null) errors.groundingWidth = `Enter an integer ${DIMENSION_MIN}–${DIMENSION_MAX}.`
  else if (width !== base.grounding_width) payload.grounding_width = width

  const height = parseIntField(groundingHeight.value, DIMENSION_MIN, DIMENSION_MAX)
  if (height === null)
    errors.groundingHeight = `Enter an integer ${DIMENSION_MIN}–${DIMENSION_MAX}.`
  else if (height !== base.grounding_height) payload.grounding_height = height

  const trimmedTemp = toText(temperature.value).trim()
  if (!trimmedTemp) {
    if (base.model_temperature !== null) payload.model_temperature = null
  } else {
    const temp = parseFloatField(trimmedTemp, TEMPERATURE_MIN, TEMPERATURE_MAX)
    if (temp === null)
      errors.temperature = `Enter a number ${TEMPERATURE_MIN}–${TEMPERATURE_MAX} or leave empty.`
    else if (base.model_temperature !== temp) payload.model_temperature = temp
  }

  const steps = parseIntField(maxSteps.value, STEPS_MIN)
  if (steps === null) errors.maxSteps = `Enter an integer ≥ ${STEPS_MIN}.`
  else if (steps !== base.max_steps) payload.max_steps = steps

  const trajectory = parseIntField(maxTrajectoryLength.value, STEPS_MIN)
  if (trajectory === null) errors.maxTrajectoryLength = `Enter an integer ≥ ${STEPS_MIN}.`
  else if (trajectory !== base.max_trajectory_length) payload.max_trajectory_length = trajectory

  const screenshot = parseIntField(screenshotMaxDimension.value, DIMENSION_MIN, DIMENSION_MAX)
  if (screenshot === null)
    errors.screenshotMaxDimension = `Enter an integer ${DIMENSION_MIN}–${DIMENSION_MAX}.`
  else if (screenshot !== base.screenshot_max_dimension)
    payload.screenshot_max_dimension = screenshot

  const pre = parseFloatField(preDelay.value, DELAY_MIN, DELAY_MAX)
  if (pre === null) errors.preDelay = `Enter a number ${DELAY_MIN}–${DELAY_MAX}.`
  else if (pre !== base.action_pre_delay) payload.action_pre_delay = pre

  const post = parseFloatField(postDelay.value, DELAY_MIN, DELAY_MAX)
  if (post === null) errors.postDelay = `Enter a number ${DELAY_MIN}–${DELAY_MAX}.`
  else if (post !== base.action_post_delay) payload.action_post_delay = post

  const wait = parseFloatField(waitDelay.value, DELAY_MIN, DELAY_MAX)
  if (wait === null) errors.waitDelay = `Enter a number ${DELAY_MIN}–${DELAY_MAX}.`
  else if (wait !== base.wait_delay) payload.wait_delay = wait

  if (enableReflection.value !== base.enable_reflection)
    payload.enable_reflection = enableReflection.value
  if (enableCodeAgent.value !== base.enable_code_agent)
    payload.enable_code_agent = enableCodeAgent.value

  return { payload, errors }
}

const dirty = computed(() => {
  const base = loaded.value
  if (!base) return false
  return (
    toText(groundingModel.value).trim() !== base.grounding_model ||
    toText(groundingWidth.value).trim() !== String(base.grounding_width) ||
    toText(groundingHeight.value).trim() !== String(base.grounding_height) ||
    toText(temperature.value).trim() !==
      (base.model_temperature == null ? '' : String(base.model_temperature)) ||
    toText(maxSteps.value).trim() !== String(base.max_steps) ||
    toText(maxTrajectoryLength.value).trim() !== String(base.max_trajectory_length) ||
    toText(screenshotMaxDimension.value).trim() !== String(base.screenshot_max_dimension) ||
    toText(preDelay.value).trim() !== String(base.action_pre_delay) ||
    toText(postDelay.value).trim() !== String(base.action_post_delay) ||
    toText(waitDelay.value).trim() !== String(base.wait_delay) ||
    enableReflection.value !== base.enable_reflection ||
    enableCodeAgent.value !== base.enable_code_agent
  )
})

async function handleSave(): Promise<void> {
  if (saving.value || !dirty.value) return
  const { payload, errors } = validate()
  fieldErrors.value = errors
  if (Object.keys(errors).length > 0) return
  if (Object.keys(payload).length === 0) return
  saving.value = true
  try {
    const saved = await saveAgentSConfig(payload)
    applyConfig(saved)
    notifications.success('Agent-S settings saved')
  } catch (e: unknown) {
    notifications.error(
      'Failed to save Agent-S settings',
      e instanceof Error ? e.message : undefined,
    )
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void loadState()
})
</script>

<template>
  <div data-testid="agent-s-config-panel" class="space-y-6">
    <div v-if="loading" class="flex justify-center py-12">
      <LoadingSpinner :size="24" />
    </div>

    <div
      v-else-if="error"
      class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      data-testid="agent-s-config-error"
    >
      {{ error }}
    </div>

    <template v-else>
      <SettingsSection
        title="Agent-S grounding"
        description="Vision grounding for click coordinates. The main Agent-S model is the Computer Use model above."
      >
        <div
          class="rounded-md border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground"
          data-testid="agent-s-main-model-hint"
        >
          The Agent-S main model is configured as the Computer Use model above — this panel only
          holds the separate grounding model and harness behavior values.
          <Button
            size="sm"
            variant="outline"
            class="ml-2"
            data-testid="agent-s-goto-computeruse"
            @click="gotoComputerUse"
          >
            Show Computer Use model
          </Button>
        </div>

        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-grounding-model" class="block text-sm font-medium">
                Grounding model
              </Label>
              <p class="text-sm text-muted-foreground">
                Empty uses the Computer Use main model as fallback.
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <ProviderModelCombobox
                input-id="agent-s-grounding-model"
                v-model="groundingModel"
                effort=""
                :models="catalog"
                empty-hint="Connect a provider under Provider & Models to browse models, or enter a provider/model id manually."
              />
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-grounding-width" class="block text-sm font-medium">
                Grounding width
              </Label>
              <p class="text-sm text-muted-foreground">
                Grounding model output coordinate width (1–7680).
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-grounding-width"
                v-model="groundingWidth"
                type="number"
                min="1"
                max="7680"
                data-testid="agent-s-grounding-width"
              />
              <p
                v-if="fieldErrors.groundingWidth"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-grounding-width"
              >
                {{ fieldErrors.groundingWidth }}
              </p>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-grounding-height" class="block text-sm font-medium">
                Grounding height
              </Label>
              <p class="text-sm text-muted-foreground">
                Grounding model output coordinate height (1–7680).
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-grounding-height"
                v-model="groundingHeight"
                type="number"
                min="1"
                max="7680"
                data-testid="agent-s-grounding-height"
              />
              <p
                v-if="fieldErrors.groundingHeight"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-grounding-height"
              >
                {{ fieldErrors.groundingHeight }}
              </p>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-temperature" class="block text-sm font-medium">
                Model temperature
              </Label>
              <p class="text-sm text-muted-foreground">Empty uses the provider default (0–2).</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-temperature"
                v-model="temperature"
                type="number"
                min="0"
                max="2"
                step="0.1"
                placeholder="Provider default"
                data-testid="agent-s-temperature"
              />
              <p
                v-if="fieldErrors.temperature"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-temperature"
              >
                {{ fieldErrors.temperature }}
              </p>
            </div>
          </div>
        </div>
      </SettingsSection>

      <SettingsSection
        title="Agent-S run behavior"
        description="Step budgets, screenshot cap, delays, and Agent-S loop features."
      >
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-max-steps" class="block text-sm font-medium">Max steps</Label>
              <p class="text-sm text-muted-foreground">Outer-loop step budget per run (min 1).</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-max-steps"
                v-model="maxSteps"
                type="number"
                min="1"
                data-testid="agent-s-max-steps"
              />
              <p
                v-if="fieldErrors.maxSteps"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-max-steps"
              >
                {{ fieldErrors.maxSteps }}
              </p>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-max-trajectory" class="block text-sm font-medium">
                Max trajectory length
              </Label>
              <p class="text-sm text-muted-foreground">Recent-step context window (min 1).</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-max-trajectory"
                v-model="maxTrajectoryLength"
                type="number"
                min="1"
                data-testid="agent-s-max-trajectory"
              />
              <p
                v-if="fieldErrors.maxTrajectoryLength"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-max-trajectory"
              >
                {{ fieldErrors.maxTrajectoryLength }}
              </p>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-screenshot-max" class="block text-sm font-medium">
                Screenshot max dimension
              </Label>
              <p class="text-sm text-muted-foreground">Screenshot long-edge cap in px (1–7680).</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-screenshot-max"
                v-model="screenshotMaxDimension"
                type="number"
                min="1"
                max="7680"
                data-testid="agent-s-screenshot-max"
              />
              <p
                v-if="fieldErrors.screenshotMaxDimension"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-screenshot-max"
              >
                {{ fieldErrors.screenshotMaxDimension }}
              </p>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <span class="block text-sm font-medium text-foreground">Reflection</span>
              <p class="text-sm text-muted-foreground">
                Let Agent-S reflect and replan between steps.
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <label class="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  v-model="enableReflection"
                  class="size-4"
                  data-testid="agent-s-enable-reflection"
                />
                Enable reflection
              </label>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <span class="block text-sm font-medium text-foreground">Code agent</span>
              <p class="text-sm text-muted-foreground">
                Allow Agent-S to run sandboxed file/data code snippets in the workspace.
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <label class="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  v-model="enableCodeAgent"
                  class="size-4"
                  data-testid="agent-s-enable-code-agent"
                />
                Enable code agent
              </label>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-pre-delay" class="block text-sm font-medium">
                Pre-action delay (s)
              </Label>
              <p class="text-sm text-muted-foreground">
                Wait before each desktop action (0–600 s).
              </p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-pre-delay"
                v-model="preDelay"
                type="number"
                min="0"
                max="600"
                step="0.5"
                data-testid="agent-s-pre-delay"
              />
              <p
                v-if="fieldErrors.preDelay"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-pre-delay"
              >
                {{ fieldErrors.preDelay }}
              </p>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-post-delay" class="block text-sm font-medium">
                Post-action delay (s)
              </Label>
              <p class="text-sm text-muted-foreground">Wait after each desktop action (0–600 s).</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-post-delay"
                v-model="postDelay"
                type="number"
                min="0"
                max="600"
                step="0.5"
                data-testid="agent-s-post-delay"
              />
              <p
                v-if="fieldErrors.postDelay"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-post-delay"
              >
                {{ fieldErrors.postDelay }}
              </p>
            </div>
          </div>

          <div
            class="flex flex-col gap-2 px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-4"
          >
            <div class="min-w-0 space-y-1">
              <Label for="agent-s-wait-delay" class="block text-sm font-medium">
                Wait delay (s)
              </Label>
              <p class="text-sm text-muted-foreground">Wait after a WAIT signal (0–600 s).</p>
            </div>
            <div class="w-full shrink-0 sm:w-80">
              <Input
                id="agent-s-wait-delay"
                v-model="waitDelay"
                type="number"
                min="0"
                max="600"
                step="0.5"
                data-testid="agent-s-wait-delay"
              />
              <p
                v-if="fieldErrors.waitDelay"
                class="mt-1 text-xs text-destructive"
                data-testid="agent-s-error-wait-delay"
              >
                {{ fieldErrors.waitDelay }}
              </p>
            </div>
          </div>
        </div>
      </SettingsSection>

      <div class="flex items-center justify-between gap-3">
        <p class="text-xs text-muted-foreground" data-testid="agent-s-config-status">
          {{ dirty ? 'Unsaved changes' : 'All changes saved' }}
        </p>
        <Button
          size="sm"
          :disabled="saving || !dirty"
          data-testid="save-agent-s-config"
          @click="handleSave"
        >
          <LoadingSpinner v-if="saving" :size="12" />
          <span v-else>Save Agent-S</span>
        </Button>
      </div>
    </template>
  </div>
</template>
