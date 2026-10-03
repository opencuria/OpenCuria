<!-- Shared model/effort menu for composers and settings. -->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowLeft, Check, ChevronDown, Search } from '@lucide/vue'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  formatEffort,
  providerDisplayName,
  resolveCatalogModel,
  snapEffort,
  type ProviderModel,
} from '@/lib/harnessModels'
import { MAX_RECENT_MODELS, type RecentModelEntry } from '@/lib/recentModels'

const props = withDefaults(
  defineProps<{
    model: string
    effort: string
    models: ProviderModel[]
    /** Catalog-known recents, newest first (max. 6); defaults to catalog head. */
    recentModels?: ProviderModel[]
    /** Last-used effort per model id (restored on selection). */
    recentEfforts?: RecentModelEntry[]
    loading?: boolean
    disabled?: boolean
    defaultModelLabel?: string
    allowDefault?: boolean
    defaultOptionLabel?: string
    defaultEffortValue?: string
    allowEffortDefault?: boolean
    defaultEffortLabel?: string
    inheritedEffortOptions?: { value: string; label: string }[]
    showEffort?: boolean
    manualFallback?: boolean
    effortFallback?: 'preserve' | 'model-default'
    variant?: 'composer' | 'field'
    inputId?: string
  }>(),
  {
    recentModels: () => [],
    recentEfforts: () => [],
    loading: false,
    disabled: false,
    defaultModelLabel: 'Select model…',
    allowDefault: false,
    defaultOptionLabel: 'Agent default',
    defaultEffortValue: '',
    defaultEffortLabel: 'Agent default',
    allowEffortDefault: undefined,
    inheritedEffortOptions: () => [],
    showEffort: true,
    manualFallback: false,
    effortFallback: 'preserve',
    variant: 'composer',
  },
)

const emit = defineEmits<{
  'update:model': [value: string]
  'update:effort': [value: string]
}>()

const open = ref(false)
const search = ref('')

watch(
  () => props.disabled,
  (disabled) => {
    if (disabled) open.value = false
  },
)
const showAll = ref(false)

const catalogModel = computed(() => resolveCatalogModel(props.models, props.model))

const effortOptions = computed(() => {
  if (!props.showEffort) return []
  if (!props.model.trim() && props.inheritedEffortOptions.length > 0)
    return props.inheritedEffortOptions
  return (catalogModel.value?.reasoning_efforts ?? []).map((value) => ({
    value,
    label: formatEffort(value),
  }))
})
const effortLabel = computed(
  () =>
    effortOptions.value.find((option) => option.value === props.effort)?.label ||
    formatEffort(props.effort) ||
    props.defaultEffortLabel,
)
const hasEffortDefault = computed(
  () =>
    (props.allowEffortDefault ?? props.allowDefault) &&
    (props.model.trim() || props.inheritedEffortOptions.length === 0),
)

const triggerModelName = computed(() => {
  if (!props.model.trim()) return props.defaultModelLabel
  return catalogModel.value?.name ?? props.model
})

/** Last-used effort lookup (lowercased tokens). */
const recentEffortById = computed(() => {
  const map = new Map<string, string>()
  for (const entry of props.recentEfforts) {
    if (entry.id && !map.has(entry.id)) map.set(entry.id, entry.effort)
  }
  return map
})

function matchesQuery(item: ProviderModel, q: string): boolean {
  const providerLabel = providerDisplayName(item.provider).toLowerCase()
  const providerId = (item.provider ?? '').toLowerCase()
  return (
    item.name.toLowerCase().includes(q) ||
    item.id.toLowerCase().includes(q) ||
    providerId.includes(q) ||
    providerLabel.includes(q)
  )
}

const trimmedQuery = computed(() => search.value.trim().toLowerCase())
const isSearching = computed(() => trimmedQuery.value.length > 0)

/**
 * Default view: the user's recent models (max. 6, catalog-known only).
 * Without history, fall back to the catalog head so the menu never opens
 * empty — the All Models button stays the path to the full catalog.
 */
const defaultRecents = computed<ProviderModel[]>(() => {
  if (props.recentModels.length > 0) return props.recentModels.slice(0, MAX_RECENT_MODELS)
  return props.models.slice(0, MAX_RECENT_MODELS)
})

const hasHistory = computed(() => props.recentModels.length > 0)

/**
 * Visible rows: a search always scans the full catalog (fast filter over
 * plain data, not the DOM). Without a query, Recent shows ≤6 rows; All
 * renders the whole catalog with light rows (no per-row tooltip).
 */
const visibleModels = computed<ProviderModel[]>(() => {
  if (isSearching.value) {
    const q = trimmedQuery.value
    return props.models.filter((item) => matchesQuery(item, q))
  }
  if (showAll.value) return props.models
  return defaultRecents.value
})

const listTitle = computed(() => {
  if (isSearching.value) return 'All models'
  return showAll.value ? 'All models' : 'Recent'
})

function rememberedEffort(id: string): string {
  return recentEffortById.value.get(id) ?? ''
}

function selectModel(id: string): void {
  if (props.disabled) return
  if (id && id.trim() === props.model.trim()) return
  if (!id && !props.model && props.inheritedEffortOptions.length > 0) return
  emit('update:model', id)
  if (!id) {
    emit('update:effort', props.defaultEffortValue)
    return
  }
  const selected = resolveCatalogModel(props.models, id)
  // Empty effort means inherit the mode's current agent default; don't fill
  // it with a model default behind the user's back. A remembered explicit
  // effort can still be restored when selecting this model again.
  const current = !props.model.trim() && props.inheritedEffortOptions.length > 0 ? '' : props.effort
  const remembered = rememberedEffort(id)
  if (remembered) emit('update:effort', selected ? snapEffort(selected, remembered) : remembered)
  else if (current || props.effortFallback === 'model-default')
    emit('update:effort', selected ? snapEffort(selected, current) : current)
}

function selectEffort(value: string): void {
  if (props.disabled) return
  emit('update:effort', value)
}

function openAll(): void {
  showAll.value = true
}

function backToRecent(): void {
  showAll.value = false
}

// Reset the view each time the menu closes so it always opens on Recent.
function onOpenChange(open: boolean): void {
  if (!open) {
    showAll.value = false
    search.value = ''
  }
}
</script>

<template>
  <DropdownMenu v-model:open="open" @update:open="onOpenChange">
    <DropdownMenuTrigger as-child :disabled="disabled">
      <Button
        type="button"
        :id="inputId"
        :variant="variant === 'field' ? 'outline' : 'ghost'"
        size="sm"
        :class="
          variant === 'field'
            ? 'h-9 w-full min-w-0 justify-between gap-2 font-normal'
            : 'h-8 min-w-0 max-w-full shrink gap-1 px-2 text-xs font-medium text-muted-foreground hover:text-foreground sm:max-w-56'
        "
        :title="triggerModelName"
        data-testid="composer-model-trigger"
        :disabled="disabled"
      >
        <span v-if="loading">Loading…</span>
        <template v-else>
          <span
            class="min-w-0 truncate"
            :class="model.trim() ? 'text-foreground' : 'text-muted-foreground'"
            >{{ triggerModelName }}</span
          >
        </template>
        <ChevronDown :size="12" class="shrink-0 opacity-70" />
      </Button>
    </DropdownMenuTrigger>
    <DropdownMenuContent
      :side="variant === 'field' ? 'bottom' : 'top'"
      align="start"
      class="w-56 min-w-56"
      data-testid="composer-model-menu"
    >
      <DropdownMenuSub v-if="effortOptions.length > 0">
        <DropdownMenuSubTrigger class="justify-between text-xs" data-testid="composer-effort-row">
          <span>Effort</span>
          <span class="text-muted-foreground">{{ effortLabel }}</span>
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent class="min-w-40" :side-offset="8">
          <DropdownMenuItem
            v-if="hasEffortDefault"
            class="text-xs"
            data-testid="composer-effort-default"
            :disabled="disabled"
            @click="selectEffort('')"
          >
            <span>{{ defaultEffortLabel }}</span>
            <Check v-if="!effort" class="ml-auto size-3.5" />
          </DropdownMenuItem>
          <DropdownMenuItem
            v-for="option in effortOptions"
            :key="option.value"
            class="text-xs"
            :data-testid="`composer-effort-${option.value}`"
            :disabled="disabled"
            @click="selectEffort(option.value)"
          >
            <span>{{ option.label }}</span>
            <Check v-if="effort === option.value" class="ml-auto size-3.5" />
          </DropdownMenuItem>
        </DropdownMenuSubContent>
      </DropdownMenuSub>
      <DropdownMenuSub>
        <DropdownMenuSubTrigger class="justify-between text-xs" data-testid="composer-model-row">
          <span>Model</span>
          <span class="max-w-28 truncate text-muted-foreground">
            {{ triggerModelName }}
          </span>
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent
          class="w-80 max-w-[calc(100vw-2rem)] p-0"
          :side-offset="8"
          data-testid="composer-model-list"
        >
          <div class="flex items-center gap-2 border-b border-border px-3 py-2.5">
            <Search class="size-3.5 shrink-0 text-muted-foreground" />
            <Input
              v-model="search"
              placeholder="Search all models…"
              class="h-6 flex-1 border-0 bg-transparent px-0 text-[13px] shadow-none focus-visible:ring-0"
              data-testid="composer-model-search"
              @keydown.stop
            />
          </div>
          <div class="max-h-64 overflow-y-auto overflow-x-hidden p-1.5">
            <DropdownMenuItem
              v-if="allowDefault"
              :disabled="disabled"
              class="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px] outline-hidden transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent"
              data-testid="composer-model-default"
              @click="selectModel('')"
            >
              <span class="min-w-0 flex-1 font-medium">{{ defaultOptionLabel }}</span>
              <Check v-if="!model" class="size-4 shrink-0 text-primary" />
            </DropdownMenuItem>
            <div v-if="models.length === 0 && manualFallback" class="p-2.5">
              <Input
                :model-value="model"
                placeholder="provider/model-id"
                aria-label="Model ID"
                data-testid="model-manual-input"
                :disabled="disabled"
                @keydown.stop
                @update:model-value="selectModel(String($event))"
              />
            </div>
            <div
              v-if="models.length > 0"
              class="flex items-center justify-between px-2.5 pb-1 pt-1.5"
            >
              <p class="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {{ listTitle }}
                <span v-if="!isSearching && !showAll && !hasHistory"> · suggestions</span>
                <span v-else-if="showAll && !isSearching" class="ml-1 normal-case tracking-normal">
                  · {{ models.length }}
                </span>
              </p>
              <button
                v-if="showAll && !isSearching"
                type="button"
                class="inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                data-testid="composer-model-show-recent"
                @click="backToRecent"
              >
                <ArrowLeft :size="12" />
                Recent
              </button>
            </div>
            <DropdownMenuItem
              v-for="item in visibleModels"
              :key="item.id"
              :disabled="disabled"
              class="flex w-full items-center gap-2 rounded-xl px-2.5 py-2 text-left text-[13px] outline-hidden transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent"
              :title="item.id"
              :data-testid="`composer-model-${item.id}`"
              @click="selectModel(item.id)"
            >
              <span class="min-w-0 flex-1">
                <span class="block truncate font-medium">{{ item.name }}</span>
                <span class="block truncate text-xs text-muted-foreground">
                  {{ providerDisplayName(item.provider) }}
                </span>
              </span>
              <Check v-if="model === item.id" class="size-4 shrink-0 text-primary" />
            </DropdownMenuItem>
            <p
              v-if="
                visibleModels.length === 0 && !loading && !(models.length === 0 && manualFallback)
              "
              class="px-2.5 py-4 text-center text-xs text-muted-foreground"
            >
              No models match.
            </p>
            <button
              v-if="!isSearching && !showAll && models.length > visibleModels.length"
              type="button"
              class="mt-1 flex w-full items-center justify-center gap-1.5 rounded-xl border border-border bg-muted/50 px-2.5 py-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              data-testid="composer-model-show-all"
              @click="openAll"
            >
              All Models ({{ models.length }})
            </button>
          </div>
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    </DropdownMenuContent>
  </DropdownMenu>
</template>
