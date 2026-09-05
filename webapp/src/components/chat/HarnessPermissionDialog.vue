<script setup lang="ts">
import { computed } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { ShieldAlert } from '@lucide/vue'
import type {
  HarnessPermissionRequest,
  HarnessPermissionResponse,
} from '@/types/harness'

const props = defineProps<{
  request: HarnessPermissionRequest | null
  resolving?: boolean
}>()

const emit = defineEmits<{
  resolve: [response: HarnessPermissionResponse]
  close: []
}>()

const open = computed(() => props.request !== null)

function handleResolve(response: HarnessPermissionResponse): void {
  emit('resolve', response)
}

function handleOpenChange(value: boolean): void {
  if (!value) emit('close')
}
</script>

<template>
  <Dialog :open="open" @update:open="handleOpenChange">
    <DialogContent>
      <DialogHeader>
        <DialogTitle class="flex items-center gap-2">
          <ShieldAlert :size="16" class="text-warning" />
          Permission required
        </DialogTitle>
        <DialogDescription>
          The agent wants to run a tool that needs approval.
        </DialogDescription>
      </DialogHeader>
      <div v-if="request" class="flex flex-col gap-3">
        <div class="flex flex-wrap items-center gap-1.5">
          <Badge variant="secondary">{{ request.tool }}</Badge>
          <Badge v-if="request.call_id" variant="outline" class="font-mono">{{ request.call_id }}</Badge>
        </div>
        <p class="text-sm font-medium text-foreground">{{ request.title }}</p>
        <pre
          v-if="request.pattern"
          class="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-xs text-muted-foreground"
        >{{ request.pattern }}</pre>
      </div>
      <DialogFooter class="flex flex-col gap-2 sm:flex-row sm:justify-end">
        <Button variant="outline" :disabled="resolving" @click="handleResolve('reject')">
          Reject
        </Button>
        <Button variant="secondary" :disabled="resolving" @click="handleResolve('once')">
          Approve once
        </Button>
        <Button :disabled="resolving" @click="handleResolve('always')">
          Always allow
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
