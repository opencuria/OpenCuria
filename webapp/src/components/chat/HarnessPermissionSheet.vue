<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronDown, ChevronUp, ShieldAlert } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { HarnessPermissionRequest, HarnessPermissionResponse } from '@/types/harness'

const props = defineProps<{
  requests: HarnessPermissionRequest[]
  resolving?: boolean
}>()

const emit = defineEmits<{
  resolve: [requestId: string, response: HarnessPermissionResponse]
}>()

const page = ref(0)
const total = computed(() => props.requests.length)
const request = computed<HarnessPermissionRequest | null>(
  () => props.requests[Math.min(page.value, Math.max(total.value - 1, 0))] ?? null,
)

watch(
  () => props.requests,
  (requests) => {
    if (page.value > requests.length - 1) {
      page.value = Math.max(0, requests.length - 1)
    }
  },
)

function handleResolve(response: HarnessPermissionResponse): void {
  if (!request.value || props.resolving) return
  emit('resolve', request.value.request_id, response)
}
</script>

<template>
  <div v-if="request" class="px-4 pb-3 pt-3" data-testid="composer-permission-sheet">
    <div class="mb-2 flex items-center gap-2">
      <ShieldAlert :size="14" class="shrink-0 text-warning" />
      <p class="text-sm font-medium text-foreground">Permission required</p>
      <div
        v-if="total > 1"
        class="ml-auto flex items-center gap-0.5"
        data-testid="composer-permission-pager"
      >
        <Button
          variant="ghost"
          size="icon-xs"
          :disabled="page <= 0 || resolving"
          title="Previous permission"
          data-testid="composer-permission-prev"
          @click="page = Math.max(0, page - 1)"
        >
          <ChevronUp :size="14" />
        </Button>
        <span
          class="min-w-10 text-center text-xs tabular-nums text-muted-foreground"
          data-testid="composer-permission-page"
        >
          {{ page + 1 }} of {{ total }}
        </span>
        <Button
          variant="ghost"
          size="icon-xs"
          :disabled="page >= total - 1 || resolving"
          title="Next permission"
          data-testid="composer-permission-next"
          @click="page = Math.min(total - 1, page + 1)"
        >
          <ChevronDown :size="14" />
        </Button>
      </div>
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
      <Button
        variant="outline"
        size="sm"
        :disabled="resolving"
        @click="handleResolve('reject')"
      >
        Reject
      </Button>
      <Button
        variant="secondary"
        size="sm"
        :disabled="resolving"
        @click="handleResolve('once')"
      >
        Approve once
      </Button>
      <Button size="sm" :disabled="resolving" @click="handleResolve('always')">
        Always allow
      </Button>
    </div>
  </div>
</template>
