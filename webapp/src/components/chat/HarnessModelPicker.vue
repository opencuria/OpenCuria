<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, ChevronDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  formatEffort,
  providerDisplayName,
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from '@/lib/harnessModels'
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip'

const props = defineProps<{
  model: string
  effort: string
  models: ProviderModel[]
  loading?: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:model': [value: string]
  'update:effort': [value: string]
}>()

const search = ref('')

const catalogModel = computed(() =>
  resolveCatalogModel(props.models, props.model),
)

const effortOptions = computed(() => catalogModel.value?.reasoning_efforts ?? [])

const triggerModelName = computed(() => {
  if (!props.model.trim()) return 'Select model…'
  return catalogModel.value?.name ?? props.model
})

const triggerEffortLabel = computed(() => {
  if (effortOptions.value.length === 0 || !props.effort) return ''
  return formatEffort(props.effort)
})

const filteredModels = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return props.models
  return props.models.filter((item) => {
    const providerLabel = providerDisplayName(item.provider).toLowerCase()
    const providerId = (item.provider ?? '').toLowerCase()
    return (
      item.name.toLowerCase().includes(q) ||
      item.id.toLowerCase().includes(q) ||
      providerId.includes(q) ||
      providerLabel.includes(q)
    )
  })
})

watch(
  () => [props.model, props.models] as const,
  () => {
    const next = snapEffort(catalogModel.value, props.effort)
    if (next !== props.effort) emit('update:effort', next)
  },
)

function selectModel(id: string): void {
  emit('update:model', id)
  const selected = resolveCatalogModel(props.models, id)
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
  <TooltipProvider>
    <DropdownMenu>
    <DropdownMenuTrigger as-child :disabled="disabled">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        class="h-8 max-w-56 gap-1 px-2 text-xs font-medium text-muted-foreground hover:text-foreground"
        data-testid="composer-model-trigger"
        :disabled="disabled"
      >
        <span v-if="loading">Loading…</span>
        <template v-else>
          <span class="min-w-0 truncate" :class="model.trim() ? 'text-foreground' : 'text-muted-foreground'">{{ triggerModelName }}</span>
          <span v-if="triggerEffortLabel" class="shrink-0 text-muted-foreground">
            {{ triggerEffortLabel }}
          </span>
        </template>
        <ChevronDown :size="12" class="shrink-0 opacity-70" />
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
            {{ model.trim() ? (catalogModel?.name ?? model) : 'Select model…' }}
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
          <div class="max-h-64 overflow-y-auto">
            <DropdownMenuItem
              v-for="item in filteredModels"
              :key="item.id"
              class="text-xs"
              :title="item.id"
              :data-testid="`composer-model-${item.id}`"
              @click="selectModel(item.id)"
            >
              <span class="min-w-0 flex-1 truncate">{{ item.name }}</span>
              <Tooltip>
                <TooltipTrigger as-child>
                  <span
                    class="ml-2 shrink-0 text-muted-foreground"
                    :data-testid="`composer-model-provider-${item.id}`"
                  >
                    {{ providerDisplayName(item.provider) }}
                  </span>
                </TooltipTrigger>
                <TooltipContent side="top">
                  {{ providerDisplayName(item.provider) }}
                </TooltipContent>
              </Tooltip>
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
  </TooltipProvider>
</template>
