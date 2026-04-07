<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { UiDialog, UiButton, UiSpinner, UiScrollArea } from '@/components/ui'
import { useWorkspaceStore } from '@/stores/workspaces'
import * as agentsApi from '@/services/agents.api'
import {
  Bot,
  Plus,
  Loader2,
  ArrowLeft,
  ChevronDown,
  ChevronUp,
  AlertCircle,
  CheckCircle,
} from 'lucide-vue-next'
import type { Workspace, Agent } from '@/types'

const router = useRouter()
const workspaceStore = useWorkspaceStore()

const open = ref(false)
const loading = ref(false)
const loadingAgents = ref(false)
const agents = ref<Agent[]>([])

// Step: 'workspace' | 'agent'
const step = ref<'workspace' | 'agent'>('workspace')
const selectedWorkspace = ref<Workspace | null>(null)
const selectedAgent = ref<Agent | null>(null)
const showAllAgents = ref(false)

const runningWorkspaces = computed(() =>
  workspaceStore.workspaces.filter(w => w.status === 'running' && w.runner_online)
)

const isInitialWorkspaceLoading = computed(() =>
  workspaceStore.loading && runningWorkspaces.value.length === 0,
)

const availableAgents = computed(() =>
  agents.value.filter(a => a.has_online_runner && a.has_credentials)
)

const secondaryAgents = computed(() =>
  agents.value.filter(a => !(a.has_online_runner && a.has_credentials))
)

function getModelChoices(agent: Agent): string[] {
  const modelOption = agent.available_options.find((option) => option.key === 'model')
  if (modelOption && modelOption.choices.length > 0) return modelOption.choices
  return []
}

onMounted(async () => {
  if (workspaceStore.workspaces.length === 0) {
    await workspaceStore.fetchWorkspaces()
  }
})

async function loadAgentsForWorkspace(workspaceId: string): Promise<void> {
  loadingAgents.value = true
  agents.value = []
  try {
    agents.value = await agentsApi.listAgents(workspaceId)
  } catch {
    agents.value = []
  } finally {
    loadingAgents.value = false
  }
}

function handleOpenChange(isOpen: boolean): void {
  open.value = isOpen
  if (!isOpen) {
    step.value = 'workspace'
    selectedWorkspace.value = null
    selectedAgent.value = null
    showAllAgents.value = false
    agents.value = []
  }
}

async function selectWorkspace(workspace: Workspace): Promise<void> {
  if (loading.value || loadingAgents.value || workspace.has_active_session || !workspace.runner_online) return
  selectedWorkspace.value = workspace
  step.value = 'agent'
  await loadAgentsForWorkspace(workspace.id)
}

async function selectAgent(agent: Agent): Promise<void> {
  if (!selectedWorkspace.value || loading.value) return
  selectedAgent.value = agent

  loading.value = true
  try {
    const workspace = selectedWorkspace.value
    const chat = await workspaceStore.createChat(workspace.id, {
      agent_definition_id: agent.id,
      agent_type: agent.name,
    })
    if (chat) {
      await router.push({
        name: 'workspace-detail',
        params: { id: workspace.id },
        query: { chatId: chat.id },
      })
    }
    open.value = false
  } finally {
    loading.value = false
    selectedAgent.value = null
  }
}

const dialogTitle = computed(() =>
  step.value === 'workspace' ? 'Start New Chat' : 'Choose Agent'
)

const dialogDescription = computed(() =>
  step.value === 'workspace'
    ? 'Select a workspace to start a new chat in'
    : `Select the AI agent for ${selectedWorkspace.value?.name || 'workspace'}`
)
</script>

<template>
  <UiDialog
    :open="open"
    :title="dialogTitle"
    :description="dialogDescription"
    @update:open="handleOpenChange"
  >
    <template #trigger>
      <slot name="trigger">
        <UiButton variant="default" size="sm">
          <Plus :size="16" class="mr-1.5" />
          New Chat
        </UiButton>
      </slot>
    </template>

    <template #default>
      <div v-if="step === 'agent'" class="flex items-center gap-2 -mt-2 mb-3">
        <button
          class="flex items-center gap-1 text-xs text-muted-fg hover:text-fg transition-colors"
          @click="step = 'workspace'"
        >
          <ArrowLeft :size="14" />
          Back to workspaces
        </button>
      </div>

      <template v-if="step === 'workspace'">
        <div v-if="isInitialWorkspaceLoading" class="flex items-center justify-center py-12">
          <UiSpinner :size="24" />
        </div>

        <div
          v-else-if="runningWorkspaces.length === 0"
          class="flex flex-col items-center justify-center py-12 text-center"
        >
          <Bot :size="40" class="text-muted-fg mb-3" />
          <p class="text-sm text-muted-fg">No running workspaces available.</p>
          <p class="text-xs text-muted-fg mt-1">Create a workspace first or start an existing one.</p>
        </div>

        <UiScrollArea v-else class="max-h-[400px]">
          <div class="space-y-2">
            <button
              v-for="workspace in runningWorkspaces"
              :key="workspace.id"
              class="w-full flex items-start gap-3 p-3 rounded-lg border transition-all text-left"
              :class="workspace.has_active_session
                ? 'border-border opacity-50 cursor-not-allowed'
                : 'border-border hover:border-primary hover:bg-primary/5'"
              :disabled="workspace.has_active_session"
              @click="selectWorkspace(workspace)"
            >
              <div class="w-9 h-9 rounded-full bg-muted flex items-center justify-center shrink-0">
                <Bot :size="16" class="text-muted-fg" />
              </div>
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium text-fg truncate mb-0.5">
                  {{ workspace.name || workspace.id.slice(0, 12) + '…' }}
                </div>
                <div class="text-xs text-muted-fg">
                  {{ workspace.runtime_type }} · {{ workspace.status }}
                </div>
                <div v-if="workspace.has_active_session" class="flex items-center gap-1 mt-1 text-xs text-warning">
                  <Loader2 :size="10" class="animate-spin" />
                  Session in progress
                </div>
              </div>
            </button>
          </div>
        </UiScrollArea>
      </template>

      <template v-else-if="step === 'agent'">
        <div v-if="loadingAgents" class="flex items-center justify-center py-12">
          <UiSpinner :size="24" />
        </div>

        <div v-else-if="availableAgents.length === 0" class="flex flex-col items-center justify-center py-12 text-center">
          <Bot :size="40" class="text-muted-fg mb-3" />
          <p class="text-sm text-muted-fg">No agents available in this workspace.</p>
          <p class="text-xs text-muted-fg mt-1">The workspace must already include the required credentials and have an online runner.</p>
        </div>

        <UiScrollArea v-else class="max-h-[420px]">
          <div class="space-y-3">
            <div class="space-y-2">
              <button
                v-for="agent in availableAgents"
                :key="agent.id"
                class="w-full flex items-start gap-3 p-3 rounded-lg border transition-all text-left"
                :class="selectedAgent?.id === agent.id
                  ? 'border-primary bg-primary/5'
                  : 'border-border hover:border-primary hover:bg-primary/5'"
                :disabled="loading"
                @click="selectAgent(agent)"
              >
                <div class="w-9 h-9 rounded-full bg-primary/10 flex items-center justify-center shrink-0">
                  <Bot :size="16" class="text-primary" />
                </div>
                <div class="flex-1 min-w-0">
                  <div class="flex items-center gap-2 mb-0.5">
                    <span class="text-sm font-medium text-fg">{{ agent.description || agent.name }}</span>
                    <CheckCircle :size="14" class="text-success shrink-0" />
                  </div>
                    <div class="text-xs text-muted-fg">
                      {{ agent.supports_multi_chat ? 'Multi-chat' : 'Single-chat' }}
                      <template v-if="getModelChoices(agent).length > 0">
                        · {{ getModelChoices(agent).length }} model{{ getModelChoices(agent).length !== 1 ? 's' : '' }}
                      </template>
                    </div>
                  </div>
                <UiSpinner v-if="loading && selectedAgent?.id === agent.id" :size="16" class="ml-auto shrink-0" />
              </button>
            </div>

            <div v-if="secondaryAgents.length > 0">
              <button
                class="flex items-center gap-2 text-xs text-muted-fg hover:text-fg transition-colors w-full py-1"
                @click="showAllAgents = !showAllAgents"
              >
                <component :is="showAllAgents ? ChevronUp : ChevronDown" :size="14" />
                {{ showAllAgents ? 'Hide' : 'Show' }} other agents ({{ secondaryAgents.length }})
              </button>

              <div v-if="showAllAgents" class="space-y-2 mt-2">
                <button
                  v-for="agent in secondaryAgents"
                  :key="agent.id"
                  class="w-full flex items-start gap-3 p-3 rounded-lg border transition-all text-left opacity-70"
                  :class="selectedAgent?.id === agent.id
                    ? 'border-primary bg-primary/5'
                    : 'border-border hover:border-muted-fg hover:bg-surface-hover'"
                  :disabled="loading"
                  @click="selectAgent(agent)"
                >
                  <div class="w-9 h-9 rounded-full bg-muted flex items-center justify-center shrink-0">
                    <Bot :size="16" class="text-muted-fg" />
                  </div>
                  <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 mb-0.5">
                      <span class="text-sm font-medium text-fg">{{ agent.description || agent.name }}</span>
                      <AlertCircle :size="14" class="text-warning shrink-0" />
                    </div>
                    <div class="text-xs text-muted-fg">
                      {{ !agent.has_online_runner ? 'No runner available' : 'Missing workspace credentials' }}
                    </div>
                  </div>
                  <UiSpinner v-if="loading && selectedAgent?.id === agent.id" :size="16" class="ml-auto shrink-0" />
                </button>
              </div>
            </div>
          </div>
        </UiScrollArea>
      </template>
    </template>
  </UiDialog>
</template>
