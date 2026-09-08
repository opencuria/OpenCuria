<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, ChevronsUpDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import {
  formatContextLength,
  formatEffort,
  providerDisplayName,
  type ProviderModel,
} from '@/lib/harnessModels'

const props = withDefaults(
  defineProps<{
    modelValue: string
    models: ProviderModel[]
    placeholder?: string
    emptyHint?: string
    manualFallback?: boolean
    inputId?: string
  }>(),
  {
    placeholder: 'Select model…',
    emptyHint: 'Connect a provider to browse models.',
    manualFallback: true,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const open = ref(false)

const selected = computed(() => props.models.find((item) => item.id === props.modelValue))

const triggerLabel = computed(() => {
  if (selected.value) return selected.value.name
  if (props.modelValue.trim()) return props.modelValue
  return props.placeholder
})

/** Models grouped by provider, preserving catalog order. */
const groupedModels = computed(() => {
  const groups = new Map<string, ProviderModel[]>()
  for (const item of props.models) {
    const key = item.provider ?? ''
    const list = groups.get(key)
    if (list) list.push(item)
    else groups.set(key, [item])
  }
  return [...groups.entries()].map(([provider, items]) => ({ provider, items }))
})

function effortHint(item: ProviderModel): string {
  if (item.default_effort) return formatEffort(item.default_effort)
  if (item.reasoning_efforts.length > 0) return formatEffort(item.reasoning_efforts[0] ?? '')
  return ''
}

function selectModel(id: string): void {
  emit('update:modelValue', id)
  open.value = false
}

function clearModel(): void {
  emit('update:modelValue', '')
  open.value = false
}
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
        </span>
        <ChevronsUpDown class="size-4 shrink-0 opacity-50" />
      </Button>
    </PopoverTrigger>
    <PopoverContent class="w-[var(--reka-popover-trigger-width)] p-0" align="start">
      <Command v-if="models.length > 0">
        <CommandInput placeholder="Search models…" />
        <CommandList>
          <CommandEmpty>No models found.</CommandEmpty>
          <CommandGroup>
            <CommandItem value="__clear__" @select="clearModel">
              <span class="text-muted-foreground">Clear selection</span>
            </CommandItem>
          </CommandGroup>
          <CommandGroup
            v-for="group in groupedModels"
            :key="group.provider || 'other'"
            :heading="providerDisplayName(group.provider) || 'Other'"
          >
            <CommandItem
              v-for="item in group.items"
              :key="item.id"
              :value="`${item.name} ${item.id} ${providerDisplayName(item.provider)}`"
              :data-testid="`model-option-${item.id}`"
              @select="selectModel(item.id)"
            >
              <span class="min-w-0 flex-1 truncate">{{ item.name }}</span>
              <span
                v-if="formatContextLength(item.context_length)"
                class="ml-2 shrink-0 font-mono text-xs text-muted-foreground"
              >
                {{ formatContextLength(item.context_length) }}
              </span>
              <span v-if="effortHint(item)" class="ml-2 shrink-0 text-xs text-muted-foreground">
                {{ effortHint(item) }}
              </span>
              <Check
                :class="
                  cn('ml-2 size-4 shrink-0', modelValue === item.id ? 'opacity-100' : 'opacity-0')
                "
              />
            </CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>
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
