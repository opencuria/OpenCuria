<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, ChevronDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  formatEffort,
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from '@/lib/harnessModels'

const props = defineProps<{
  model: string
  effort: string
  models: ProviderModel[]
  loading?: boolean
  defaultModel?: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:model': [value: string]
  'update:effort': [value: string]
}>()

const search = ref('')

const catalogModel = computed(() =>
  resolveCatalogModel(props.models, props.model, props.defaultModel ?? ''),
)

const effortOptions = computed(() => catalogModel.value?.reasoning_efforts ?? [])

const triggerLabel = computed(() => {
  if (effortOptions.value.length > 0 && props.effort) {
    return formatEffort(props.effort)
  }
  if (!props.model.trim()) return 'Auto'
  return catalogModel.value?.name ?? props.model
})

const filteredModels = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return props.models
  return props.models.filter(
    (item) => item.name.toLowerCase().includes(q) || item.id.toLowerCase().includes(q),
  )
})

watch(
  () => [props.model, props.defaultModel, props.models] as const,
  () => {
    const next = snapEffort(catalogModel.value, props.effort)
    if (next !== props.effort) emit('update:effort', next)
  },
)

function selectModel(id: string): void {
  emit('update:model', id)
  const selected = resolveCatalogModel(props.models, id, props.defaultModel ?? '')
  emit('update:effort', snapEffort(selected, props.effort))
}

function selectEffort(value: string): void {
  emit('update:effort', value)
}

function modelEffortHint(item: ProviderModel): string {
  if (item.default_effort) return formatEffort(item.default_effort)
  if (item.reasoning_efforts.length > 0) return formatEffort(item.reasoning_efforts[0] ?? '')
  return ''
}
</script>

<template>
  <DropdownMenu>
    <DropdownMenuTrigger as-child :disabled="disabled">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        class="h-8 gap-1 px-2 text-xs font-medium text-muted-foreground hover:text-foreground"
        data-testid="composer-model-trigger"
        :disabled="disabled"
      >
        {{ loading ? 'Loading…' : triggerLabel }}
        <ChevronDown :size="12" class="opacity-70" />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent side="top" align="start" class="w-56 min-w-56" data-testid="composer-model-menu">
      <DropdownMenuSub v-if="effortOptions.length > 0">
        <DropdownMenuSubTrigger class="justify-between text-xs" data-testid="composer-effort-row">
          <span>Effort</span>
          <span class="text-muted-foreground">{{ formatEffort(effort) }}</span>
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent class="min-w-40">
          <DropdownMenuItem
            v-for="option in effortOptions"
            :key="option"
            class="text-xs"
            :data-testid="`composer-effort-${option}`"
            @click="selectEffort(option)"
          >
            <span>{{ formatEffort(option) }}</span>
            <Check v-if="effort === option" class="ml-auto size-3.5" />
          </DropdownMenuItem>
        </DropdownMenuSubContent>
      </DropdownMenuSub>
      <DropdownMenuSub>
        <DropdownMenuSubTrigger class="justify-between text-xs" data-testid="composer-model-row">
          <span>Model</span>
          <span class="max-w-28 truncate text-muted-foreground">
            {{ model.trim() ? (catalogModel?.name ?? model) : 'Auto' }}
          </span>
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent class="w-72 p-1" data-testid="composer-model-list">
          <div class="px-1 pb-1" @keydown.stop>
            <Input
              v-model="search"
              placeholder="Search models"
              class="h-8 text-xs"
              data-testid="composer-model-search"
            />
          </div>
          <DropdownMenuItem class="text-xs" data-testid="composer-model-auto" @click="selectModel('')">
            <span>Auto</span>
            <Check v-if="!model.trim()" class="ml-auto size-3.5" />
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <div class="max-h-64 overflow-y-auto">
            <DropdownMenuItem
              v-for="item in filteredModels"
              :key="item.id"
              class="text-xs"
              :title="item.id"
              @click="selectModel(item.id)"
            >
              <span class="min-w-0 flex-1 truncate">{{ item.name }}</span>
              <span v-if="modelEffortHint(item)" class="ml-2 shrink-0 text-muted-foreground">
                {{ modelEffortHint(item) }}
              </span>
              <Check v-if="model === item.id" class="ml-auto size-3.5 shrink-0" />
            </DropdownMenuItem>
            <p
              v-if="filteredModels.length === 0 && !loading"
              class="px-3 py-2 text-xs text-muted-foreground"
            >
              No models match.
            </p>
          </div>
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    </DropdownMenuContent>
  </DropdownMenu>
</template>
