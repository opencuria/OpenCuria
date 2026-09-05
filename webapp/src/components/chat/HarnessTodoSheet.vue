<script setup lang="ts">
import { computed } from 'vue'
import { CircleCheck, CircleDashed, CircleX, ChevronDown, ListTodo, Loader2 } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import type { HarnessTodo } from '@/types/harness'

const props = defineProps<{
  todos: HarnessTodo[]
  /** Collapsed when rendered as a peek edge below a higher-priority sheet. */
  peek?: boolean
}>()

const open = defineModel<boolean>('open', { default: true })

const sorted = computed(() => [...props.todos].sort((a, b) => a.order - b.order))

const doneCount = computed(() => props.todos.filter((todo) => todo.status === 'completed').length)

function statusVariant(
  status: HarnessTodo['status'],
): 'secondary' | 'outline' | 'default' | 'destructive' {
  switch (status) {
    case 'completed':
      return 'secondary'
    case 'in_progress':
      return 'default'
    case 'cancelled':
      return 'destructive'
    default:
      return 'outline'
  }
}
</script>

<template>
  <Collapsible v-model:open="open" :disabled="peek" data-testid="composer-todo-sheet">
    <CollapsibleTrigger
      class="flex w-full items-center gap-2 px-4 pt-3 text-left"
      :class="peek ? 'cursor-default' : ''"
      data-testid="composer-todo-trigger"
    >
      <ListTodo :size="14" class="shrink-0 text-muted-foreground" />
      <span class="text-sm font-medium text-foreground">Todos</span>
      <Badge variant="secondary" class="ml-auto" data-testid="composer-todo-count">
        {{ doneCount }}/{{ todos.length }}
      </Badge>
      <ChevronDown
        :size="14"
        class="shrink-0 text-muted-foreground transition-transform"
        :class="open ? 'rotate-180' : ''"
      />
    </CollapsibleTrigger>
    <CollapsibleContent class="px-4 pb-3 pt-2">
      <ul class="flex max-h-48 flex-col gap-1.5 overflow-y-auto">
        <li
          v-for="todo in sorted"
          :key="todo.id"
          class="flex items-start gap-2 text-sm"
          data-testid="composer-todo-row"
        >
          <Loader2
            v-if="todo.status === 'in_progress'"
            :size="14"
            class="mt-0.5 shrink-0 animate-spin text-primary"
          />
          <CircleCheck
            v-else-if="todo.status === 'completed'"
            :size="14"
            class="mt-0.5 shrink-0 text-emerald-600 dark:text-emerald-400"
          />
          <CircleX
            v-else-if="todo.status === 'cancelled'"
            :size="14"
            class="mt-0.5 shrink-0 text-muted-foreground"
          />
          <CircleDashed v-else :size="14" class="mt-0.5 shrink-0 text-muted-foreground" />
          <span
            class="min-w-0 flex-1 break-words"
            :class="
              todo.status === 'completed' ? 'text-muted-foreground line-through' : 'text-foreground'
            "
          >
            {{ todo.content }}
          </span>
          <Badge :variant="statusVariant(todo.status)" class="shrink-0">
            {{ todo.status.replace('_', ' ') }}
          </Badge>
        </li>
      </ul>
    </CollapsibleContent>
  </Collapsible>
</template>
