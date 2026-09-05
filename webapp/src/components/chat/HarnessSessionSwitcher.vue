<script setup lang="ts">
import { computed, ref } from 'vue'
import { useHarnessStore } from '@/stores/harness'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import HarnessChatInput from '@/components/chat/HarnessChatInput.vue'
import type { HarnessSession, HarnessSessionMode } from '@/types/harness'
import { Plus } from '@lucide/vue'

const props = defineProps<{
  workspaceId: string
}>()

const emit = defineEmits<{
  create: [prompt: string, mode: HarnessSessionMode, model: string]
}>()

const harness = useHarnessStore()
const dialogOpen = ref(false)
const composerMode = ref<HarnessSessionMode>('build')

function sortedSessions(sessions: HarnessSession[]): HarnessSession[] {
  return [...sessions].sort((a, b) => {
    const aTime = a.updated_at ? new Date(a.updated_at).getTime() : 0
    const bTime = b.updated_at ? new Date(b.updated_at).getTime() : 0
    return bTime - aTime
  })
}

const orderedSessions = computed(() => sortedSessions(harness.sessions))

function shortTitle(session: HarnessSession): string {
  return session.title || 'Untitled session'
}
</script>

<template>
  <div class="flex items-center gap-2 border-b border-border px-3 py-2 sm:px-4">
    <select
      v-if="orderedSessions.length > 0"
      :value="harness.activeSessionId ?? ''"
      class="h-8 min-w-0 flex-1 rounded-md border border-input bg-background px-2 text-xs text-foreground"
      aria-label="Harness session"
      @change="
        harness.setActiveSession(($event.target as HTMLSelectElement).value || null)
      "
    >
      <option
        v-for="session in orderedSessions"
        :key="session.id"
        :value="session.id"
      >
        {{ shortTitle(session) }}
      </option>
    </select>
    <span v-else class="min-w-0 flex-1 truncate text-xs text-muted-foreground">
      No chats yet — start a new one
    </span>
    <Button
      variant="outline"
      size="sm"
      class="shrink-0"
      title="Start a new harness chat"
      @click="dialogOpen = true"
    >
      <Plus :size="14" />
      New Chat
    </Button>

    <Dialog :open="dialogOpen" @update:open="dialogOpen = $event">
      <DialogContent class="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Start a new harness chat</DialogTitle>
        </DialogHeader>
        <HarnessChatInput
          :workspace-id="props.workspaceId"
          :mode="composerMode"
          :model="harness.modelInput"
          @update:mode="composerMode = $event"
          @update:model="harness.modelInput = $event"
          @send="
            (prompt, mode, model) => {
              dialogOpen = false
              emit('create', prompt, mode, model)
            }
          "
        />
        <DialogFooter />
      </DialogContent>
    </Dialog>
  </div>
</template>
