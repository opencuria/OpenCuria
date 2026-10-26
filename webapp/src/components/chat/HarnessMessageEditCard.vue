<!-- Inline editor for a user message: rerun from here with an optional new model. -->
<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { CornerDownLeft } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import ModelPicker from '@/components/common/ModelPicker.vue'
import {
  harnessEngineDefaults,
  harnessSessionModels,
  type ProviderModel,
} from '@/lib/harnessModels'
import { loadEngineModelsCached } from '@/lib/providerCatalog'
import { loadRecentModels, recentCatalogModels, useRecentModels } from '@/lib/recentModels'
import { useHarnessStore } from '@/stores/harness'

const props = defineProps<{
  text: string
  sessionId: string
  /** A run is active: rerunning stops and discards it after confirmation. */
  busy?: boolean
}>()

const emit = defineEmits<{
  cancel: []
  submit: [text: string, selection: { model: string; effort: string }]
}>()

const harness = useHarnessStore()
const session = computed(() => harness.sessions.find((item) => item.id === props.sessionId))
const harnessId = computed(() => session.value?.harness_id ?? 'native')

const draft = ref(props.text)
// Start from the session binding, like the composer does for this session.
const defaults = harnessEngineDefaults(harnessId.value)
const model = ref(session.value?.model || defaults.model)
const effort = ref(session.value?.reasoning_effort ?? defaults.effort)

const engineCatalog = ref<ProviderModel[]>([])
const modelLoading = ref(true)
const models = computed(() =>
  harnessSessionModels(engineCatalog.value, harnessId.value, session.value?.model ?? ''),
)
const { entries: recentEntries } = useRecentModels()
const recentModels = computed(() =>
  harnessId.value !== 'claude' ? recentCatalogModels(models.value) : [],
)

const confirmOpen = ref(false)
const input = ref<{ $el: HTMLTextAreaElement } | null>(null)

onMounted(async () => {
  void nextTick(() => {
    const el = input.value?.$el
    if (!el) return
    el.focus()
    el.setSelectionRange(el.value.length, el.value.length)
  })
  const engineId = harnessId.value
  try {
    const [loaded] = await Promise.all([
      loadEngineModelsCached(engineId),
      engineId !== 'claude' ? loadRecentModels() : Promise.resolve([]),
    ])
    engineCatalog.value = loaded
  } catch {
    engineCatalog.value = []
  } finally {
    modelLoading.value = false
  }
})

/** Enter reruns, Shift+Enter breaks the line; never steal Enter from IME composition. */
function onEnter(event: KeyboardEvent): void {
  if (event.isComposing) return
  event.preventDefault()
  rerun()
}

function rerun(): void {
  if (!draft.value.trim()) return
  if (props.busy) {
    confirmOpen.value = true
    return
  }
  submit()
}

function confirmRerun(): void {
  confirmOpen.value = false
  submit()
}

function submit(): void {
  emit('submit', draft.value.trim(), { model: model.value, effort: effort.value })
}
</script>

<template>
  <div
    data-testid="message-edit-card"
    class="flex flex-col rounded-2xl border border-border bg-card shadow-xs transition-[border-color,box-shadow] focus-within:border-primary/50 focus-within:ring-3 focus-within:ring-primary/15"
  >
    <Textarea
      ref="input"
      v-model="draft"
      aria-label="Edit message"
      class="max-h-[50vh] min-h-12 overflow-y-auto rounded-none border-0 bg-transparent px-3.5 pb-1 pt-3 text-sm leading-relaxed shadow-none focus-visible:ring-0 dark:bg-transparent"
      data-testid="message-edit-input"
      @keydown.escape="emit('cancel')"
      @keydown.enter.exact="onEnter"
    />
    <div class="flex items-center justify-between gap-2 py-2 pl-1.5 pr-2">
      <div class="flex min-w-0 items-center gap-1" data-testid="message-edit-model">
        <ModelPicker
          :model="model"
          :effort="effort"
          :models="models"
          :recent-models="recentModels"
          :recent-efforts="harnessId !== 'claude' ? recentEntries : []"
          :loading="modelLoading"
          @update:model="model = $event"
          @update:effort="effort = $event"
        />
        <span class="hidden truncate text-[11px] text-muted-foreground sm:inline">
          Reruns from here
        </span>
      </div>
      <div class="flex shrink-0 items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          class="h-7 px-2.5 text-xs text-muted-foreground hover:text-foreground"
          data-testid="message-edit-cancel"
          @click="emit('cancel')"
        >
          Cancel
        </Button>
        <Button
          type="button"
          size="sm"
          class="h-7 gap-1.5 px-2.5 text-xs"
          data-testid="message-edit-save"
          :disabled="!draft.trim()"
          @click="rerun"
        >
          Rerun
          <CornerDownLeft :size="12" aria-hidden="true" class="opacity-70" />
        </Button>
      </div>
    </div>
    <Dialog v-model:open="confirmOpen">
      <DialogContent data-testid="message-edit-confirm" :show-close-button="false">
        <DialogHeader>
          <DialogTitle>Stop the running session?</DialogTitle>
          <DialogDescription>
            The agent is still working. Rerunning from this message stops the current run and
            discards it along with every message after this one.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            data-testid="message-edit-confirm-cancel"
            @click="confirmOpen = false"
          >
            Keep running
          </Button>
          <Button
            type="button"
            variant="destructive"
            data-testid="message-edit-confirm-rerun"
            @click="confirmRerun"
          >
            Stop &amp; rerun
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
