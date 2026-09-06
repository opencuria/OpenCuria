<script setup lang="ts">
/**
 * ChatHomeView — zentrierter Home-Screen (Route `/`, name `home`).
 *
 * OpenWebUI-Placeholder-Layout: Greeting + WorkspacePicker-Pill +
 * wiederverwendeter HarnessChatInput + Suggestion-Chips.
 * Kein Polling: ChatSidebar übernimmt Live-Updates; hier reicht ein
 * einmaliges fetchWorkspaces()/fetchSkills() beim Mount.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Container, Plus } from '@lucide/vue'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import HarnessChatInput from '@/components/chat/HarnessChatInput.vue'
import CreateWorkspaceDialog from '@/components/workspaces/CreateWorkspaceDialog.vue'
import WorkspacePicker from '@/components/workspaces/WorkspacePicker.vue'
import { Button } from '@/components/ui/button'
import type { HarnessSessionMode } from '@/types/harness'
import { WorkspaceStatus } from '@/types'
import { useAuthStore } from '@/stores/auth'
import { useHarnessStore } from '@/stores/harness'
import { useSkillStore } from '@/stores/skills'
import { useWorkspaceStore } from '@/stores/workspaces'

const LAST_WORKSPACE_KEY = 'opencuria:last-workspace'

const router = useRouter()
const authStore = useAuthStore()
const harnessStore = useHarnessStore()
const skillStore = useSkillStore()
const workspaceStore = useWorkspaceStore()

const selectedWorkspaceId = ref<string | null>(null)
const sending = ref(false)
const composerMode = ref<HarnessSessionMode>('build')
const createOpen = ref(false)

interface HomeSuggestion {
  title: string
  subtitle: string
  prompt: string
}

const suggestions: HomeSuggestion[] = [
  {
    title: 'Neues Projekt starten',
    subtitle: 'Idee beschreiben, Struktur vorschlagen lassen',
    prompt: 'Hilf mir, ein neues Projekt zu starten: ',
  },
  {
    title: 'Code erklären lassen',
    subtitle: 'Unbekannte Datei oder Funktion verstehen',
    prompt: 'Erkläre mir folgenden Code: ',
  },
  {
    title: 'Bug fixen',
    subtitle: 'Fehlerbild schildern, Ursache finden',
    prompt: 'Hilf mir, diesen Fehler zu beheben: ',
  },
  {
    title: 'Dokumentation schreiben',
    subtitle: 'README oder Kommentare entwerfen',
    prompt: 'Schreibe eine Dokumentation für: ',
  },
]

const greetingName = computed(() => {
  const email = authStore.user?.email ?? ''
  const prefix = email.split('@')[0]?.trim() ?? ''
  return prefix
})

const readyWorkspace = computed(() => {
  const workspace = workspaceStore.workspaces.find(
    (entry) => entry.id === selectedWorkspaceId.value,
  )
  if (
    workspace &&
    workspace.status === WorkspaceStatus.RUNNING &&
    workspace.runner_online &&
    !workspace.active_operation
  ) {
    return workspace
  }
  return null
})

const hasWorkspaces = computed(() => workspaceStore.workspaces.length > 0)

const busyMessage = computed(() => {
  if (!hasWorkspaces.value) return ''
  if (!selectedWorkspaceId.value) return 'Wähle einen laufenden Workspace'
  const workspace = workspaceStore.workspaces.find(
    (entry) => entry.id === selectedWorkspaceId.value,
  )
  if (!workspace) return 'Wähle einen laufenden Workspace'
  if (workspace.status !== WorkspaceStatus.RUNNING || !workspace.runner_online) {
    return 'Workspace ist nicht bereit — Runner offline oder gestoppt'
  }
  if (workspace.active_operation) {
    return workspaceStore.getWorkspaceTransitionLabel(workspace.id) ?? 'Workspace ist beschäftigt…'
  }
  return ''
})

const inputDisabled = computed(() => sending.value || !readyWorkspace.value)

function pickInitialWorkspace(): void {
  const workspaces = workspaceStore.workspaces
  if (!workspaces.length) {
    selectedWorkspaceId.value = null
    return
  }
  const stored = localStorage.getItem(LAST_WORKSPACE_KEY)
  if (stored && workspaces.some((entry) => entry.id === stored)) {
    selectedWorkspaceId.value = stored
    return
  }
  const firstReady = workspaces.find(
    (entry) => entry.status === WorkspaceStatus.RUNNING && entry.runner_online,
  )
  selectedWorkspaceId.value = firstReady?.id ?? workspaces[0]?.id ?? null
}

function persistSelection(id: string | null): void {
  if (id) {
    localStorage.setItem(LAST_WORKSPACE_KEY, id)
  } else {
    localStorage.removeItem(LAST_WORKSPACE_KEY)
  }
}

function handleSelectionChange(id: string | null): void {
  selectedWorkspaceId.value = id
  persistSelection(id)
}

function handleCreatedWorkspace(id: string | null): void {
  if (!id) return
  void workspaceStore.fetchWorkspaces().then(() => {
    selectedWorkspaceId.value = id
    persistSelection(id)
  })
}

async function handleSend(
  prompt: string,
  mode: HarnessSessionMode,
  model: string,
  skillIds: string[],
  effort: string,
): Promise<void> {
  const workspace = readyWorkspace.value
  if (!workspace || sending.value) return
  sending.value = true
  try {
    const session = await harnessStore.createSession(
      workspace.id,
      prompt,
      mode,
      model,
      skillIds,
      effort,
    )
    const sessionId = session?.id ?? harnessStore.activeSessionId
    if (sessionId) {
      await router.push({
        name: 'workspace-detail',
        params: { id: workspace.id },
        query: { session: sessionId },
      })
    }
  } finally {
    sending.value = false
  }
}

function handleSuggestion(suggestion: HomeSuggestion): void {
  void handleSend(
    suggestion.prompt,
    composerMode.value,
    harnessStore.modelInput,
    [],
    harnessStore.effortInput,
  )
}

onMounted(async () => {
  if (workspaceStore.workspaces.length === 0) {
    await workspaceStore.fetchWorkspaces()
  }
  pickInitialWorkspace()
  void skillStore.fetchSkills()
})
</script>

<template>
  <div class="flex min-h-0 flex-1 flex-col overflow-y-auto" data-testid="chat-home">
    <div class="m-auto w-full max-w-3xl px-4 py-16 text-center sm:py-24">
      <div class="mb-4 flex justify-center">
        <OpenCuriaLogo icon-only alt="OpenCuria" class="size-12" />
      </div>

      <h1 class="text-2xl font-medium text-foreground" data-testid="chat-home-greeting">
        <template v-if="greetingName">Wie kann ich helfen, {{ greetingName }}?</template>
        <template v-else>Wie kann ich helfen?</template>
      </h1>

      <div class="mt-4 flex justify-center">
        <WorkspacePicker
          :model-value="selectedWorkspaceId"
          @update:model-value="handleSelectionChange"
        />
      </div>

      <div
        v-if="!hasWorkspaces"
        class="mx-auto mt-6 flex max-w-md flex-col items-center gap-3 rounded-lg border border-border bg-card px-6 py-8"
        data-testid="chat-home-empty"
      >
        <Container :size="20" class="text-muted-foreground" aria-hidden="true" />
        <p class="text-sm text-muted-foreground">
          Noch kein Workspace vorhanden. Erstelle einen Workspace, um zu starten.
        </p>
        <Button size="sm" data-testid="chat-home-create" @click="createOpen = true">
          <Plus :size="14" aria-hidden="true" />
          Workspace erstellen
        </Button>
        <CreateWorkspaceDialog v-model:open="createOpen" @created="handleCreatedWorkspace">
          <span class="hidden" aria-hidden="true" />
        </CreateWorkspaceDialog>
      </div>

      <div v-else class="mt-6">
        <HarnessChatInput
          :workspace-id="selectedWorkspaceId ?? undefined"
          :session-id="null"
          :mode="composerMode"
          :model="harnessStore.modelInput"
          :effort="harnessStore.effortInput"
          :skill-options="skillStore.skills"
          :disabled="inputDisabled"
          :sending="sending"
          :busy-message="busyMessage"
          class="text-left"
          data-testid="chat-home-composer"
          @update:mode="composerMode = $event"
          @update:model="harnessStore.modelInput = $event"
          @update:effort="harnessStore.effortInput = $event"
          @send="handleSend"
        />

        <div
          class="mt-4 grid grid-cols-1 gap-2 text-left sm:grid-cols-2"
          data-testid="chat-home-suggestions"
        >
          <button
            v-for="(suggestion, idx) in suggestions"
            :key="suggestion.title"
            type="button"
            :style="{ animationDelay: `${idx * 45}ms` }"
            :disabled="inputDisabled"
            :aria-label="`${suggestion.title} — ${suggestion.subtitle}`"
            data-testid="chat-home-suggestion"
            class="animate-[harness-fade-up_0.35s_ease-out_both] rounded-lg border border-border bg-card px-3 py-2.5 text-left transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
            @click="handleSuggestion(suggestion)"
          >
            <span class="block truncate text-sm font-medium text-foreground">
              {{ suggestion.title }}
            </span>
            <span class="block truncate text-xs text-muted-foreground">
              {{ suggestion.subtitle }}
            </span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
@keyframes harness-fade-up {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
