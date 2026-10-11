<script setup lang="ts">
import { computed } from 'vue'
import { ChevronDown } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { runnerActivityGroups, runnerActivityLabel } from '@/lib/runnerActivity'
import { formatRelativeTime } from '@/lib/utils'
import RunnerOperationsPanel from './RunnerOperationsPanel.vue'
import type { RunnerStorage, DeletionRequest, DeletionTarget } from '@/types/runnerStorage'

const props = defineProps<{ data: RunnerStorage; deletions: DeletionRequest[]; busy?: boolean }>()
const emit = defineEmits<{
  changed: []
  cancel: [id: string]
  review: [target: DeletionTarget, name: string]
}>()
const groups = computed(() => {
  const grouped = runnerActivityGroups(props.data.activities)
  return [
    { key: 'attention', title: 'Needs attention', entries: grouped.attention },
    { key: 'active', title: 'In progress', entries: grouped.active },
    { key: 'history', title: 'History', entries: grouped.history },
  ]
})
const hasActivity = computed(() => props.data.activities?.length)
const deletion = (id: string) => props.deletions.find((request) => request.id === id)
function cancel(id: string) {
  if (!props.busy && deletion(id)?.can_cancel) emit('cancel', id)
}
function review(id: string, name: string) {
  const request = deletion(id)
  if (!props.busy && request?.phase === 'reconfirmation_required')
    emit('review', { target_type: request.target_type, target_id: request.target_id }, name)
}
</script>

<template>
  <div id="runner-operations" class="min-w-0 space-y-4">
    <p v-if="!hasActivity" class="text-sm text-muted-foreground">
      {{
        data.activities
          ? 'No recorded activity'
          : 'Activity status unavailable. Refresh to try again.'
      }}
    </p>
    <template v-for="group in groups" :key="group.key">
      <Collapsible v-if="group.entries.length" :open="group.key === 'history' ? undefined : true">
        <CollapsibleTrigger v-if="group.key === 'history'" as-child>
          <Button size="sm" variant="ghost"
            ><ChevronDown :size="14" /> History ({{ group.entries.length }})</Button
          >
        </CollapsibleTrigger>
        <h3 v-else class="mb-2 text-sm font-semibold">{{ group.title }}</h3>
        <CollapsibleContent>
          <section :aria-label="group.title" class="divide-y rounded-md border">
            <article
              v-for="activity in group.entries"
              :key="activity.id"
              class="min-w-0 space-y-2 p-3"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span class="break-words text-sm font-medium">{{ activity.target_name }}</span>
                <Badge
                  variant="secondary"
                  :class="activity.state === 'attention' ? 'text-warning' : ''"
                >
                  {{ runnerActivityLabel(activity) }}
                </Badge>
                <span class="text-xs text-muted-foreground">{{ activity.title }}</span>
                <span v-if="activity.created_at" class="ml-auto text-xs text-muted-foreground">
                  {{ formatRelativeTime(activity.created_at) }}
                </span>
              </div>
              <p class="text-xs text-muted-foreground">{{ activity.summary }}</p>
              <RunnerOperationsPanel
                v-if="activity.operation_ids.length"
                :operations="
                  data.operations.filter((op) => activity.operation_ids.includes(op.task_id))
                "
                :diagnostic="activity.diagnostic"
                @changed="emit('changed')"
              />
              <div
                v-if="activity.kind === 'deletion' && activity.state !== 'history'"
                class="flex flex-wrap gap-2"
              >
                <Button
                  v-if="deletion(activity.id)?.can_cancel"
                  size="sm"
                  variant="outline"
                  :disabled="busy"
                  @click="cancel(activity.id)"
                >
                  Cancel request
                </Button>
                <Button
                  v-if="deletion(activity.id)?.phase === 'reconfirmation_required'"
                  size="sm"
                  variant="outline"
                  :disabled="busy"
                  @click="review(activity.id, activity.target_name)"
                >
                  Review deletion
                </Button>
              </div>
              <Collapsible v-if="!activity.operation_ids.length && activity.diagnostic">
                <CollapsibleTrigger as-child
                  ><Button size="sm" variant="ghost"
                    ><ChevronDown :size="14" /> Details</Button
                  ></CollapsibleTrigger
                >
                <CollapsibleContent
                  ><p class="break-words text-xs text-muted-foreground">
                    {{ activity.diagnostic }}
                  </p></CollapsibleContent
                >
              </Collapsible>
            </article>
          </section>
        </CollapsibleContent>
      </Collapsible>
    </template>
  </div>
</template>
