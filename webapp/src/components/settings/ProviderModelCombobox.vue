<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, ChevronsUpDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import {
  formatEffort,
  providerDisplayName,
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from '@/lib/harnessModels'

const MAX_VISIBLE_ROWS = 150

const props = withDefaults(
  defineProps<{
    modelValue: string
    models: ProviderModel[]
    effort?: string
    placeholder?: string
    emptyHint?: string
    manualFallback?: boolean
    inputId?: string
  }>(),
  {
    effort: '',
    placeholder: 'Select model…',
    emptyHint: 'Connect a provider to browse models.',
    manualFallback: true,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:effort': [value: string]
}>()

const open = ref(false)
const search = ref('')

const selected = computed(() => resolveCatalogModel(props.models, props.modelValue))

const selectedEffortLabel = computed(() => {
  const effort = props.modelValue.trim() ? props.effort : ''
  const efforts = selected.value?.reasoning_efforts ?? []
  if (!effort.trim() || efforts.length === 0) return ''
  return formatEffort(effort)
})

const triggerLabel = computed(() => {
  if (selected.value) return selected.value.name
  if (props.modelValue.trim()) return props.modelValue
  return props.placeholder
})

/** Search across name, id, provider id/label — analog to HarnessModelPicker. */
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

/** Models grouped by provider, preserving catalog order. */
const filteredGroups = computed(() => {
  const groups = new Map<string, ProviderModel[]>()
  for (const item of filteredModels.value) {
    const key = item.provider ?? ''
    const list = groups.get(key)
    if (list) list.push(item)
    else groups.set(key, [item])
  }
  return [...groups.entries()].map(([provider, items]) => ({ provider, items }))
})

/** Cap rendered rows across groups for many-model catalogs. */
const visibleGroups = computed(() => {
  const out: { provider: string; items: ProviderModel[] }[] = []
  let count = 0
  for (const group of filteredGroups.value) {
    if (count >= MAX_VISIBLE_ROWS) break
    const items = group.items.slice(0, MAX_VISIBLE_ROWS - count)
    count += items.length
    out.push({ provider: group.provider, items })
  }
  return out
})

const truncatedCount = computed(() => filteredModels.value.length - MAX_VISIBLE_ROWS)

/** Displayed effort per row: live prop for the selected model, default/first otherwise. */
function rowEffort(item: ProviderModel): string {
  if (item.id === props.modelValue && props.effort) return props.effort
  return snapEffort(item, '')
}

function selectModel(id: string): void {
  emit('update:modelValue', id)
  const next = resolveCatalogModel(props.models, id)
  emit('update:effort', snapEffort(next, props.effort))
  open.value = false
}

function clearModel(): void {
  emit('update:modelValue', '')
  emit('update:effort', '')
  open.value = false
}

function changeRowEffort(item: ProviderModel, value: string): void {
  if (item.id !== props.modelValue) {
    emit('update:modelValue', item.id)
  }
  emit('update:effort', value)
  open.value = false
}

watch(
  () => [props.modelValue, props.models] as const,
  () => {
    const next = snapEffort(selected.value, props.effort)
    if (next !== props.effort) emit('update:effort', next)
  },
)
</script>

<template>
  <Popover v-model:open="open">
    <PopoverTrigger as-child>
      <Button
        :id="inputId"
        type="button"
        variant="outline"
        role="combobox"
        :aria-expanded="open"
        :title="triggerLabel"
        class="w-full justify-between font-normal"
        :data-testid="inputId ? `${inputId}-trigger` : undefined"
      >
        <span class="min-w-0 flex-1 truncate text-left">
          <span :class="cn(!modelValue.trim() && 'text-muted-foreground')">
            {{ triggerLabel }}
          </span>
          <span v-if="selected" class="ml-1.5 text-muted-foreground">
            {{ providerDisplayName(selected.provider) }}
          </span>
          <span v-if="selectedEffortLabel" class="ml-1.5 text-muted-foreground">
            {{ selectedEffortLabel }}
          </span>
        </span>
        <ChevronsUpDown class="size-4 shrink-0 opacity-50" />
      </Button>
    </PopoverTrigger>
    <PopoverContent class="w-80 p-1 sm:w-96 max-w-[calc(100vw-2rem)]" align="start">
      <div v-if="models.length > 0" class="flex flex-col">
        <div class="p-1" @keydown.stop>
          <Input
            v-model="search"
            placeholder="Search models…"
            data-testid="model-search"
          />
        </div>
        <div class="max-h-64 overflow-y-auto">
          <button
            type="button"
            class="flex w-full items-center rounded-md px-2 py-1.5 text-sm text-muted-foreground hover:bg-muted"
            data-testid="model-option-clear"
            @click="clearModel"
          >
            Clear selection
          </button>
          <div v-for="group in visibleGroups" :key="group.provider || 'other'">
            <p
              class="px-2 pb-1 pt-2 text-xs font-medium text-muted-foreground"
              data-testid="group-heading"
            >
              {{ providerDisplayName(group.provider) || 'Other' }}
            </p>
            <div
              v-for="item in group.items"
              :key="item.id"
              :title="item.id"
              role="option"
              :aria-selected="modelValue === item.id"
              class="flex items-start gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted"
              :data-testid="`model-option-${item.id}`"
              @click="selectModel(item.id)"
            >
              <span class="min-w-0 flex-1 whitespace-normal break-words leading-snug">
                {{ item.name }}
              </span>
              <select
                v-if="item.reasoning_efforts.length > 0"
                :value="rowEffort(item)"
                class="ml-auto h-7 max-w-28 shrink-0 rounded-md border border-input bg-background px-1 text-xs"
                :data-testid="`model-effort-${item.id}`"
                @click.stop
                @change="changeRowEffort(item, ($event.target as HTMLSelectElement).value)"
              >
                <option
                  v-for="option in item.reasoning_efforts"
                  :key="option"
                  :value="option"
                >
                  {{ formatEffort(option) }}
                </option>
              </select>
              <Check
                :class="
                  cn('mt-0.5 size-4 shrink-0', modelValue === item.id ? 'opacity-100' : 'opacity-0')
                "
              />
            </div>
          </div>
          <p
            v-if="filteredModels.length === 0"
            class="px-2 py-2 text-sm text-muted-foreground"
          >
            No models found.
          </p>
          <p
            v-if="truncatedCount > 0"
            class="px-2 py-2 text-xs text-muted-foreground"
            data-testid="model-list-more"
          >
            Showing first {{ MAX_VISIBLE_ROWS }} of {{ filteredModels.length }} — refine your search
          </p>
        </div>
      </div>
      <div v-else-if="manualFallback" class="space-y-2 p-3">
        <Input
          :model-value="modelValue"
          placeholder="provider/model-id"
          @update:model-value="emit('update:modelValue', String($event))"
        />
        <p class="text-xs text-muted-foreground">{{ emptyHint }}</p>
      </div>
      <p v-else class="p-3 text-xs text-muted-foreground">{{ emptyHint }}</p>
    </PopoverContent>
  </Popover>
</template>
