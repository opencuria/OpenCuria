<script setup lang="ts">
import type { Workspace } from '@/types'
import WorkspaceCard from './WorkspaceCard.vue'
import { Container } from '@lucide/vue'

defineProps<{
  workspaces: Workspace[]
}>()

const emit = defineEmits<{
  select: [workspace: Workspace]
}>()
</script>

<template>
  <div v-if="workspaces.length" class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
    <div v-for="workspace in workspaces" :key="workspace.id" class="flex flex-col gap-2">
      <WorkspaceCard :workspace="workspace" @click="emit('select', workspace)" />
      <div v-if="$slots.actions" class="flex justify-end">
        <slot name="actions" :workspace="workspace" />
      </div>
    </div>
  </div>

  <div
    v-else
    class="flex flex-col items-center justify-center py-12 px-6 text-center"
  >
    <div class="mb-4 text-muted-foreground">
      <Container :size="40" />
    </div>
    <h3 class="text-lg font-medium text-foreground mb-1">No workspaces</h3>
    <p class="text-sm text-muted-foreground max-w-sm">
      Create a workspace to start running AI coding agents on your repositories.
    </p>
  </div>
</template>
