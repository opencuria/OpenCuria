<script setup lang="ts">
import { ShieldAlert } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { HarnessPermissionRequest, HarnessPermissionResponse } from '@/types/harness'

defineProps<{
  request: HarnessPermissionRequest
  resolving?: boolean
}>()

const emit = defineEmits<{
  resolve: [response: HarnessPermissionResponse]
}>()
</script>

<template>
  <div class="px-4 pb-3 pt-3" data-testid="composer-permission-sheet">
    <div class="mb-2 flex items-center gap-2">
      <ShieldAlert :size="14" class="shrink-0 text-warning" />
      <p class="text-sm font-medium text-foreground">Permission required</p>
    </div>
    <p class="text-xs text-muted-foreground">The agent wants to run a tool that needs approval.</p>
    <div class="mt-2 flex flex-wrap items-center gap-1.5">
      <Badge variant="secondary">{{ request.tool }}</Badge>
      <Badge v-if="request.call_id" variant="outline" class="font-mono">{{
        request.call_id
      }}</Badge>
    </div>
    <p class="mt-2 text-sm font-medium text-foreground">{{ request.title }}</p>
    <pre
      v-if="request.pattern"
      class="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-xs text-muted-foreground"
      >{{ request.pattern }}</pre
    >
    <div class="mt-3 flex flex-col gap-2 sm:flex-row sm:justify-end">
      <Button variant="outline" size="sm" :disabled="resolving" @click="emit('resolve', 'reject')">
        Reject
      </Button>
      <Button variant="secondary" size="sm" :disabled="resolving" @click="emit('resolve', 'once')">
        Approve once
      </Button>
      <Button size="sm" :disabled="resolving" @click="emit('resolve', 'always')">
        Always allow
      </Button>
    </div>
  </div>
</template>
