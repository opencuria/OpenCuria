<script setup lang="ts">
import type { Workspace } from '@/types'
import WorkspaceCard from './WorkspaceCard.vue'

defineProps<{
  workspaces: Workspace[]
  warningWorkspaceIds?: Record<string, boolean>
}>()

const emit = defineEmits<{
  select: [workspace: Workspace]
}>()
</script>

<template>
  <div class="grid auto-rows-fr grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3"
  >
    <div v-for="workspace in workspaces" :key="workspace.id" class="flex min-w-0 flex-col gap-2">
      <WorkspaceCard
        :workspace="workspace"
        :show-resource-warning="warningWorkspaceIds?.[workspace.id]"
        @click="emit('select', workspace)"
      />
      <div v-if="$slots.actions" class="flex justify-end">
        <slot name="actions" :workspace="workspace" />
      </div>
    </div>
  </div>
</template>
