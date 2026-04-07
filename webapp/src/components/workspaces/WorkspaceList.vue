<script setup lang="ts">
import type { Workspace } from '@/types'
import WorkspaceCard from './WorkspaceCard.vue'
import { UiEmptyState } from '@/components/ui'
import { Container } from 'lucide-vue-next'
import { useRouter } from 'vue-router'

defineProps<{
  workspaces: Workspace[]
  warningWorkspaceIds?: Record<string, boolean>
}>()

const router = useRouter()

function openWorkspace(id: string): void {
  router.push(`/workspaces/${id}`)
}
</script>

<template>
  <div v-if="workspaces.length" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
    <WorkspaceCard
      v-for="ws in workspaces"
      :key="ws.id"
      :workspace="ws"
      :show-resource-warning="Boolean(warningWorkspaceIds?.[ws.id])"
      clickable
      @click="openWorkspace(ws.id)"
    />
  </div>

  <UiEmptyState
    v-else
    title="No workspaces"
    description="Create a workspace to start running AI coding agents on your repositories."
  >
    <template #icon>
      <Container :size="40" />
    </template>
  </UiEmptyState>
</template>
