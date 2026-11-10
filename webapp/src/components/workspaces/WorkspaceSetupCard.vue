<!--
  WorkspaceSetupCard — first-run guide on the home screen while the user has no
  workspace. Admins see the real setup state (runner online, image built);
  members get the create action, held back while no image is ready.
-->
<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { Check } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  OPEN_SETTINGS_EVENT,
  SETTINGS_CLOSED_EVENT,
  type SettingsTabId,
} from '@/components/settings/settingsTabs'
import { usePolling } from '@/composables/usePolling'
import { useWorkspaceImageReadiness } from '@/composables/useWorkspaceImageReadiness'
import { useAuthStore } from '@/stores/auth'
import { useRunnerStore } from '@/stores/runners'

const emit = defineEmits<{ create: [] }>()

const authStore = useAuthStore()
const runnerStore = useRunnerStore()
const { checked, readyImages, onlineRunner, buildableDefinition, error, ready, check } =
  useWorkspaceImageReadiness()

/** Members wait for an image; an unknown state (failed check) does not block. */
const memberBlocked = computed(() => checked.value && !ready.value && !error.value)

interface SetupStep {
  id: 'runner' | 'image' | 'workspace'
  title: string
  detail: string
  done: boolean
  action: string
  tab?: SettingsTabId
}

const steps = computed<SetupStep[]>(() => {
  const runner = runnerStore.runners[0]
  const image = readyImages.value[0]
  return [
    {
      id: 'runner',
      title: 'Connect a runner',
      detail: onlineRunner.value
        ? `${onlineRunner.value.name} is online.`
        : runner
          ? `${runner.name} is offline. Start it on its host.`
          : 'Register a runner on a KVM host.',
      done: onlineRunner.value !== null,
      action: 'Runners',
      tab: 'runners',
    },
    {
      id: 'image',
      title: 'Build an image',
      detail: image
        ? `${image.label} is ready.`
        : buildableDefinition.value
          ? `Build ${buildableDefinition.value.name} on your runner.`
          : 'Add an image definition and build it.',
      done: image !== undefined,
      action: 'Images',
      tab: 'image-definitions',
    },
    {
      id: 'workspace',
      title: 'Create a workspace',
      detail: 'Name it and choose the repositories to clone.',
      done: false,
      action: 'Create',
    },
  ]
})
const currentStepId = computed(() => steps.value.find((step) => !step.done)?.id ?? 'workspace')
const doneCount = computed(() => steps.value.filter((step) => step.done).length)

function runStep(step: SetupStep): void {
  if (step.tab) {
    window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab: step.tab } }))
  } else {
    emit('create')
  }
}

// Setup happens elsewhere (Settings, the runner host, another admin); keep the
// state current and re-check right away when Settings closes.
const { start, refresh } = usePolling(check, 15_000)
onMounted(() => {
  start()
  window.addEventListener(SETTINGS_CLOSED_EVENT, refresh)
})
onUnmounted(() => window.removeEventListener(SETTINGS_CLOSED_EVENT, refresh))
</script>

<template>
  <section
    class="mx-auto w-full max-w-lg overflow-hidden rounded-xl border border-border bg-card text-left shadow-[0_1px_2px_rgb(0_0_0/0.04),0_12px_32px_-16px_rgb(0_0_0/0.16)]"
    data-testid="workspace-setup"
  >
    <!-- Wraps the action below the text once the card gets narrow. -->
    <header class="flex flex-wrap items-start gap-x-4 gap-y-3 px-5 pt-5 pb-4">
      <div class="min-w-0 flex-1 basis-64">
        <h2 class="text-[15px] font-semibold text-foreground">Set up your first workspace</h2>
        <p
          class="mt-1 text-sm text-pretty text-muted-foreground"
          data-testid="workspace-setup-description"
        >
          <template v-if="!authStore.isAdmin && memberBlocked">
            No workspace image is available yet. You can create a workspace as soon as one is
            ready.
          </template>
          <template v-else>Agents work in isolated VMs with a terminal, desktop and Git.</template>
        </p>
      </div>
      <span
        v-if="authStore.isAdmin && checked"
        class="mt-0.5 shrink-0 text-xs text-muted-foreground tabular-nums"
        data-testid="workspace-setup-progress"
        >{{ doneCount }} of {{ steps.length }}</span
      >
      <Button
        v-if="!authStore.isAdmin"
        class="shrink-0"
        :disabled="memberBlocked"
        data-testid="workspace-setup-create"
        @click="emit('create')"
        >Create workspace</Button
      >
    </header>

    <ol v-if="authStore.isAdmin" class="divide-y divide-border border-t border-border">
      <li
        v-for="(step, index) in steps"
        :key="step.id"
        class="flex items-center gap-3 px-5 py-3.5"
        :data-testid="`workspace-setup-step-${step.id}`"
        :data-state="step.done ? 'done' : step.id === currentStepId ? 'current' : 'upcoming'"
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
          <p v-if="checked || !step.tab" class="text-xs text-pretty text-muted-foreground">
            {{ step.detail }}
          </p>
          <div v-else class="mt-1 h-3 w-36 animate-pulse rounded bg-muted" />
        </div>
        <Button
          v-if="!step.tab || (checked && step.id === currentStepId)"
          size="sm"
          :variant="step.id === currentStepId && checked ? 'default' : 'outline'"
          class="shrink-0"
          :data-testid="`workspace-setup-action-${step.id}`"
          @click="runStep(step)"
          >{{ step.action }}</Button
        >
      </li>
    </ol>
  </section>
</template>
