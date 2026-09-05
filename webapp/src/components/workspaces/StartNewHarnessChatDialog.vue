<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Bot, ArrowLeft } from '@lucide/vue'
import type { HarnessSessionMode } from '@/types/harness'
import type { Workspace } from '@/types'
import { WorkspaceStatus } from '@/types'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useHarnessStore } from '@/stores/harness'
import HarnessChatInput from '@/components/chat/HarnessChatInput.vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'

const router = useRouter()
const workspaceStore = useWorkspaceStore()
const harnessStore = useHarnessStore()

const open = ref(false)
const step = ref<'workspace' | 'prompt'>('workspace')
const selectedWorkspace = ref<Workspace | null>(null)
const composerMode = ref<HarnessSessionMode>('build')
const creating = ref(false)

const runningWorkspaces = computed(() =>
  workspaceStore.workspaces.filter(
    (workspace) =>
      workspace.status === WorkspaceStatus.RUNNING && workspace.runner_online,
  ),
)

onMounted(async () => {
  if (workspaceStore.workspaces.length === 0) {
    await workspaceStore.fetchWorkspaces()
  }
})

function resetDialog(): void {
  step.value = 'workspace'
  selectedWorkspace.value = null
  composerMode.value = 'build'
  creating.value = false
}

function handleOpenChange(isOpen: boolean): void {
  open.value = isOpen
  if (!isOpen) resetDialog()
}

function selectWorkspace(workspace: Workspace): void {
  selectedWorkspace.value = workspace
  step.value = 'prompt'
}

async function handleCreateSession(
  prompt: string,
  mode: HarnessSessionMode,
  model: string,
  skillIds: string[],
): Promise<void> {
  const workspace = selectedWorkspace.value
  if (!workspace || creating.value) return
  creating.value = true
  try {
    const session = await harnessStore.createSession(
      workspace.id,
      prompt,
      mode,
      model,
      skillIds,
    )
    if (session) {
      open.value = false
      await router.push({
        name: 'workspace-detail',
        params: { id: workspace.id },
        query: { session: session.id },
      })
    }
  } finally {
    creating.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="handleOpenChange">
    <DialogTrigger as-child>
      <slot name="trigger" />
    </DialogTrigger>
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>
          {{ step === 'workspace' ? 'Start New Chat' : `New chat in ${selectedWorkspace?.name}` }}
        </DialogTitle>
      </DialogHeader>

      <div v-if="step === 'workspace'" class="space-y-3">
        <p class="text-sm text-muted-foreground">
          Choose a running workspace to start a harness session.
        </p>
        <ScrollArea class="max-h-72">
          <div class="flex flex-col gap-2 pr-3">
            <button
              v-for="workspace in runningWorkspaces"
              :key="workspace.id"
              type="button"
              class="w-full rounded-lg border border-border bg-background px-3 py-2 text-left hover:bg-accent transition-colors"
              @click="selectWorkspace(workspace)"
            >
              <div class="text-sm font-medium text-foreground">{{ workspace.name }}</div>
              <div class="text-xs text-muted-foreground font-mono">
                {{ workspace.id.slice(0, 12) }}…
              </div>
            </button>
            <p
              v-if="runningWorkspaces.length === 0"
              class="py-6 text-center text-sm text-muted-foreground"
            >
              No running workspaces available.
            </p>
          </div>
        </ScrollArea>
      </div>

      <div v-else-if="selectedWorkspace" class="space-y-3">
        <Button variant="ghost" size="sm" class="px-0" @click="step = 'workspace'">
          <ArrowLeft :size="14" class="mr-1" />
          Back
        </Button>
        <HarnessChatInput
          :workspace-id="selectedWorkspace.id"
          :mode="composerMode"
          :model="harnessStore.modelInput"
          :disabled="creating"
          @update:mode="composerMode = $event"
          @update:model="harnessStore.modelInput = $event"
          @send="handleCreateSession"
        />
      </div>

      <DialogFooter />
    </DialogContent>
  </Dialog>
</template>
