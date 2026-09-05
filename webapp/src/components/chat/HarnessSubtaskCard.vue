<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { Badge } from '@/components/ui/badge'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import { ChevronDown, Network } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import HarnessMarkdown from './HarnessMarkdown.vue'

const props = defineProps<{
  part: HarnessPart
  childSessionId?: string | null
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
}>()

const open = ref(false)

/** The child session id links parent/child (backend `parent` FK). */
const childId = computed(() => {
  const fromMeta = props.part.meta?.['child_session_id']
  if (typeof fromMeta === 'string' && fromMeta) return fromMeta
  return props.childSessionId ?? null
})

const badgeVariant = computed(() => {
  switch (props.part.state) {
    case 'completed':
      return 'secondary'
    case 'error':
      return 'destructive'
    case 'running':
      return 'default'
    default:
      return 'outline'
  }
})

const agent = computed(() => {
  const raw = props.part.meta?.['agent']
  return typeof raw === 'string' && raw ? raw : null
})

function handleOpen(): void {
  if (childId.value) emit('openSubtask', childId.value)
}
</script>

<template>
  <Collapsible v-model:open="open" class="w-full rounded-xl border border-border bg-card">
    <CollapsibleTrigger class="flex w-full items-center gap-2 px-3 py-2 text-left text-sm">
      <Network :size="14" class="shrink-0 text-muted-foreground" />
      <span class="min-w-0 flex-1 truncate font-medium text-foreground">
        {{ part.title || 'Subagent task' }}
      </span>
      <Badge v-if="agent" variant="outline" class="shrink-0">{{ agent }}</Badge>
      <Badge :variant="badgeVariant" class="shrink-0">
        <LoadingSpinner v-if="part.state === 'running'" :size="10" />
        {{ part.state }}
      </Badge>
      <ChevronDown
        :size="14"
        class="shrink-0 text-muted-foreground transition-transform"
        :class="open ? 'rotate-180' : ''"
      />
    </CollapsibleTrigger>
    <CollapsibleContent class="px-3 pb-3">
      <div v-if="part.output" class="mt-1">
        <HarnessMarkdown :text="part.output" compact />
      </div>
      <p v-else-if="part.state === 'running'" class="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
        <LoadingSpinner :size="12" />
        Subagent is running…
      </p>
      <button
        v-if="childId"
        type="button"
        class="mt-2 text-xs font-medium text-primary hover:underline"
        @click="handleOpen"
      >
        Open child session {{ childId.slice(0, 8) }}
      </button>
    </CollapsibleContent>
  </Collapsible>
</template>
