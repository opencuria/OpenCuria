<script setup lang="ts">
import { computed } from 'vue'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { CircleCheck, CircleDashed, Loader2, ListTodo, CircleX } from '@lucide/vue'
import type { HarnessTodo } from '@/types/harness'

const props = defineProps<{
  todos: HarnessTodo[]
}>()

const sorted = computed(() =>
  [...props.todos].sort((a, b) => a.order - b.order),
)

const doneCount = computed(
  () => props.todos.filter((t) => t.status === 'completed').length,
)

function statusVariant(status: HarnessTodo['status']): 'secondary' | 'outline' | 'default' | 'destructive' {
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
  <Card size="sm" class="w-full max-w-3xl">
    <CardHeader class="flex flex-row items-center gap-2 space-y-0">
      <ListTodo :size="14" class="text-muted-foreground" />
      <CardTitle class="text-sm font-medium">Todos</CardTitle>
      <Badge variant="secondary" class="ml-auto">{{ doneCount }}/{{ todos.length }}</Badge>
    </CardHeader>
    <CardContent>
      <ul class="flex flex-col gap-1.5">
        <li v-for="todo in sorted" :key="todo.id" class="flex items-start gap-2 text-sm">
          <Loader2 v-if="todo.status === 'in_progress'" :size="14" class="mt-0.5 shrink-0 animate-spin text-primary" />
          <CircleCheck v-else-if="todo.status === 'completed'" :size="14" class="mt-0.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
          <CircleX v-else-if="todo.status === 'cancelled'" :size="14" class="mt-0.5 shrink-0 text-muted-foreground" />
          <CircleDashed v-else :size="14" class="mt-0.5 shrink-0 text-muted-foreground" />
          <span
            class="min-w-0 flex-1 break-words"
            :class="todo.status === 'completed' ? 'text-muted-foreground line-through' : 'text-foreground'"
          >
            {{ todo.content }}
          </span>
          <Badge :variant="statusVariant(todo.status)" class="shrink-0">
            {{ todo.status.replace('_', ' ') }}
          </Badge>
        </li>
      </ul>
    </CardContent>
  </Card>
</template>
