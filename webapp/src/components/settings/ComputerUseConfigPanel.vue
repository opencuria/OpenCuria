<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'
import AutosaveStatus from './AutosaveStatus.vue'
import { useAutosave } from '@/composables/useAutosave'
import { getComputerUseConfig, saveComputerUseConfig, type ComputerUseConfig, type ComputerUseConfigIn } from '@/services/harness.api'

const loading = ref(true)
const error = ref('')
const loaded = ref<ComputerUseConfig | null>(null)
const maxSteps = ref('15')
const screenshotSize = ref('2400')
const recording = ref(false)
const validation = computed(() => {
  const errors: Record<string, string> = {}
  const payload: ComputerUseConfigIn = {}
  const base = loaded.value
  if (!base) return { errors, payload }
  for (const [key, raw, min, max] of [
    ['max_steps', maxSteps.value, 1, 1000],
    ['screenshot_max_dimension', screenshotSize.value, 320, 7680],
  ] as const) {
    const value = Number(raw)
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(value) || value < min || value > max) {
      errors[key] = `Enter an integer ${min}–${max}.`
    } else if (value !== base[key]) payload[key] = value
  }
  if (recording.value !== base.enable_recording) payload.enable_recording = recording.value
  return { errors, payload }
})
const autosave = useAutosave({
  source: () => [maxSteps.value, screenshotSize.value, recording.value],
  dirty: () => Object.keys(validation.value.payload).length > 0 || Object.keys(validation.value.errors).length > 0,
  enabled: () => !loading.value && !!loaded.value && !error.value,
  valid: () => Object.keys(validation.value.errors).length === 0,
  errorMessage: 'Failed to save Computer Use settings',
  save: async (isCurrent) => {
    const payload = { ...validation.value.payload }
    if (!Object.keys(payload).length) return
    const saved = await saveComputerUseConfig(payload)
    if (isCurrent()) loaded.value = saved
  },
})
onMounted(async () => {
  try {
    const config = await getComputerUseConfig()
    loaded.value = config
    maxSteps.value = String(config.max_steps)
    screenshotSize.value = String(config.screenshot_max_dimension)
    recording.value = config.enable_recording
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load Computer Use settings'
  } finally { loading.value = false }
})
</script>

<template>
  <div data-testid="computer-use-config-panel" class="space-y-4">
    <LoadingSpinner v-if="loading" :size="24" />
    <p v-else-if="error" role="alert" class="text-sm text-destructive">{{ error }}</p>
    <SettingsSection v-else title="Computer Use" description="Activate the Computer Use plugin in a workspace to control its desktop.">
      <div class="divide-y divide-border rounded-lg border border-border bg-card">
        <div class="flex items-center justify-between gap-4 px-4 py-4">
          <Label for="computer-use-max-steps">Subagent step limit</Label>
          <div class="w-40">
            <Input id="computer-use-max-steps" v-model="maxSteps" type="number" min="1" max="1000" />
            <p v-if="validation.errors.max_steps" class="text-sm text-destructive">{{ validation.errors.max_steps }}</p>
          </div>
        </div>
        <div class="flex items-center justify-between gap-4 px-4 py-4">
          <Label for="computer-use-screenshot-size">Maximum screenshot size</Label>
          <div class="w-40">
            <Input id="computer-use-screenshot-size" v-model="screenshotSize" type="number" min="320" max="7680" />
            <p v-if="validation.errors.screenshot_max_dimension" class="text-sm text-destructive">{{ validation.errors.screenshot_max_dimension }}</p>
          </div>
        </div>
        <div class="flex items-center justify-between gap-4 px-4 py-4">
          <div class="space-y-1">
            <Label for="computer-use-recording">Record desktop sessions</Label>
            <p class="text-sm text-muted-foreground">Save a video of automated desktop activity.</p>
          </div>
          <Switch id="computer-use-recording" v-model="recording" />
        </div>
      </div>
      <AutosaveStatus :state="autosave" />
    </SettingsSection>
  </div>
</template>
