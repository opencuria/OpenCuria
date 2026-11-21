<!--
  WorkspaceSetupCard — first-run guide on the home screen while the user has no
  workspace. Steps and their state come from chat-core (`workspaceSetup`):
  admins see runner, image, provider and workspace; members only the provider
  and workspace steps they can act on themselves.
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Check } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  OPEN_SETTINGS_EVENT,
  SETTINGS_CLOSED_EVENT,
  type SettingsTabId,
} from '@/components/settings/settingsTabs'
import { usePolling } from '@/composables/usePolling'
import { useWorkspaceImageReadiness } from '@/composables/useWorkspaceImageReadiness'
import { loadEngineModelsCached } from '@/lib/providerCatalog'
import {
  HARNESS_ENGINE_CONNECTION_CHANGED_EVENT,
  listHarnessEngines,
} from '@/services/harness.api'
import { useAuthStore } from '@/stores/auth'
import { useImageStore } from '@/stores/images'
import { useRunnerStore } from '@/stores/runners'
import {
  currentWorkspaceSetupStep,
  isModelProviderConnected,
  workspaceSetupSteps,
  type WorkspaceSetupStep,
  type WorkspaceSetupStepId,
} from '@opencuria/chat-core/workspaceSetup'

const emit = defineEmits<{ create: [] }>()

const authStore = useAuthStore()
const runnerStore = useRunnerStore()
const imageStore = useImageStore()
const { checked, error, check: checkImages } = useWorkspaceImageReadiness()

const providerConnected = ref(false)

/** Where each step's action leads; the workspace step opens the create dialog. */
const STEP_TABS: Partial<Record<WorkspaceSetupStepId, SettingsTabId>> = {
  runner: 'runners',
  image: 'image-definitions',
  provider: 'provider',
}

const steps = computed(() =>
  workspaceSetupSteps({
    isAdmin: authStore.isAdmin,
    runners: runnerStore.runners,
    definitions: imageStore.imageDefinitions,
    buildsByDefinition: imageStore.runnerBuildsByDefinition,
    capturedImages: imageStore.capturedImages,
    imagesUnknown: error.value !== null,
    providerConnected: providerConnected.value,
  }),
)
const currentStepId = computed(() => currentWorkspaceSetupStep(steps.value)?.id ?? null)
const doneCount = computed(() => steps.value.filter((step) => step.done).length)

function stepState(step: WorkspaceSetupStep): 'done' | 'current' | 'upcoming' {
  if (step.done) return 'done'
  return step.id === currentStepId.value ? 'current' : 'upcoming'
}

function runStep(step: WorkspaceSetupStep): void {
  const tab = STEP_TABS[step.id]
  if (tab) {
    window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab } }))
  } else {
    emit('create')
  }
}

async function checkProvider(): Promise<void> {
  // A failed catalog reads as "not connected"; the step then offers the fix.
  const [models, engines] = await Promise.all([
    loadEngineModelsCached('native').catch(() => []),
    listHarnessEngines().catch(() => []),
  ])
  providerConnected.value = isModelProviderConnected(models, engines)
}

async function check(): Promise<void> {
  await Promise.all([checkImages(), checkProvider()])
}

// Setup happens elsewhere (Settings, the runner host, another admin); keep the
// state current and re-check right away when Settings closes or providers change.
const { start, refresh } = usePolling(check, 15_000)
onMounted(() => {
  start()
  window.addEventListener(SETTINGS_CLOSED_EVENT, refresh)
  window.addEventListener(HARNESS_ENGINE_CONNECTION_CHANGED_EVENT, refresh)
})
onUnmounted(() => {
  window.removeEventListener(SETTINGS_CLOSED_EVENT, refresh)
  window.removeEventListener(HARNESS_ENGINE_CONNECTION_CHANGED_EVENT, refresh)
})
</script>

<template>
  <section
    class="mx-auto w-full max-w-lg overflow-hidden rounded-xl border border-border bg-card text-left shadow-[0_1px_2px_rgb(0_0_0/0.04),0_12px_32px_-16px_rgb(0_0_0/0.16)]"
    data-testid="workspace-setup"
  >
    <header class="flex items-start gap-4 px-5 pt-5 pb-4">
      <div class="min-w-0 flex-1">
        <h2 class="text-[15px] font-semibold text-foreground">Set up your first workspace</h2>
        <p
          class="mt-1 text-sm text-pretty text-muted-foreground"
          data-testid="workspace-setup-description"
        >
          Agents work in isolated VMs with a terminal, desktop and Git.
        </p>
      </div>
      <span
        v-if="checked"
        class="mt-0.5 shrink-0 text-xs text-muted-foreground tabular-nums"
        data-testid="workspace-setup-progress"
        >{{ doneCount }} of {{ steps.length }}</span
      >
    </header>

    <ol class="divide-y divide-border border-t border-border">
      <li
        v-for="(step, index) in steps"
        :key="step.id"
        class="flex items-center gap-3 px-5 py-3.5"
        :data-testid="`workspace-setup-step-${step.id}`"
        :data-state="stepState(step)"
      >
        <span
          class="flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold tabular-nums transition-colors duration-200"
          :class="
            step.done
              ? 'bg-success/12 text-success'
              : step.id === currentStepId && checked
                ? 'border-[1.5px] border-primary text-primary'
                : 'border border-border text-muted-foreground'
          "
          aria-hidden="true"
        >
          <Check v-if="step.done" class="size-3.5" :stroke-width="2.5" />
          <template v-else>{{ index + 1 }}</template>
        </span>
        <div class="min-w-0 flex-1">
          <p
            class="text-sm font-medium"
            :class="step.done ? 'text-muted-foreground' : 'text-foreground'"
          >
            {{ step.title }}<span v-if="step.done" class="sr-only"> (done)</span>
          </p>
          <p v-if="checked" class="text-xs text-pretty text-muted-foreground">
            {{ step.detail }}
          </p>
          <div v-else class="mt-1 h-3 w-36 animate-pulse rounded bg-muted" />
        </div>
        <!-- Every step that can run now offers its action; the next one leads. -->
        <Button
          v-if="checked && !step.done && step.available"
          size="sm"
          :variant="step.id === currentStepId ? 'default' : 'outline'"
          class="shrink-0"
          :data-testid="`workspace-setup-action-${step.id}`"
          @click="runStep(step)"
          >{{ step.action }}</Button
        >
      </li>
    </ol>
  </section>
</template>
