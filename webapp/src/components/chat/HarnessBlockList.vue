<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { HarnessPart } from '@/types/harness'
import type { RenderBlock } from '@/lib/harnessBlocks'
import type { ProviderModel } from '@/lib/harnessModels'
import HarnessMarkdown from './HarnessMarkdown.vue'
import HarnessQuestionCard from './HarnessQuestionCard.vue'
import HarnessWorkRow from './HarnessWorkRow.vue'
import HarnessWorkedGroup from './HarnessWorkedGroup.vue'
import HarnessSubtaskCard from './HarnessSubtaskCard.vue'
import HarnessPatchCard from './HarnessPatchCard.vue'
import { isQuestionToolPart } from '@/lib/harnessBlocks'
import HarnessCompactionRow from './HarnessCompactionRow.vue'
import HarnessPageControls from './HarnessPageControls.vue'
import { HARNESS_PAGE_SIZE, lastHarnessPage, shouldResetHarnessPage } from '@/lib/harnessPagination'

const props = defineProps<{
  blocks: RenderBlock[]
  showStreamingCursor?: boolean
  childSessionIds?: Record<string, string>
  models?: ProviderModel[]
}>()

const emit = defineEmits<{
  openSubtask: [childSessionId: string]
}>()

function childIdFor(part: HarnessPart): string | null {
  const fromMeta = part.meta?.['child_session_id']
  if (typeof fromMeta === 'string' && fromMeta) return fromMeta
  const fromMap =
    props.childSessionIds?.[String(part.meta?.['subtask_id'] ?? '')] ??
    props.childSessionIds?.[part.id]
  return fromMap ?? null
}

const page = ref(props.showStreamingCursor ? lastHarnessPage(props.blocks.length) : 0)
const visibleEntries = computed(() => {
  const start = page.value * HARNESS_PAGE_SIZE
  const end = (page.value + 1) * HARNESS_PAGE_SIZE
  const entries = props.blocks
    .slice(start, end)
    .map((block, index) => ({ block, index: start + index }))
  const lastIndex = props.blocks.length - 1
  const lastBlock = props.blocks[lastIndex]
  if (
    props.showStreamingCursor &&
    lastBlock?.kind === 'text' &&
    (lastIndex < start || lastIndex >= end)
  ) {
    entries.push({ block: lastBlock, index: lastIndex })
  }
  return entries
})

function partIds(block: RenderBlock): string[] {
  if (block.kind === 'group') return block.parts.map((part) => part.id)
  if (block.kind === 'compaction') return [block.part.id]
  return [block.part.id]
}

function blockIdentity(block: RenderBlock): string {
  if (block.kind === 'group') return `work:${block.parts[0]?.id ?? ''}`
  if (block.kind === 'single' && block.part.type === 'tool') return `work:${block.part.id}`
  return `${block.kind}:${partIds(block).join(',')}`
}

const blockIdentities = computed(() => props.blocks.map(blockIdentity))
watch(
  () => props.blocks.length,
  (length, previous) => {
    if (props.showStreamingCursor && length > previous) page.value = lastHarnessPage(length)
  },
)
watch(
  () => props.showStreamingCursor,
  (streaming) => {
    if (streaming) page.value = lastHarnessPage(props.blocks.length)
  },
)
watch(blockIdentities, (next, previous) => {
  if (shouldResetHarnessPage(previous, next)) {
    page.value = props.showStreamingCursor ? lastHarnessPage(next.length) : 0
  } else {
    page.value = Math.min(page.value, lastHarnessPage(next.length))
  }
})

function blockKey(block: RenderBlock, index: number): string {
  if (!block) return `block-${index}`
  if (block.kind === 'group') {
    return `group-${block.parts[0]?.id ?? index}`
  }
  if (block.kind === 'compaction') {
    return `compaction-${block.part.id}`
  }
  return `${block.kind}-${block.part.id}`
}
</script>

<template>
  <div class="flex flex-col gap-2">
    <HarnessPageControls
      :page="page"
      :total-items="blocks.length"
      label="Worked blocks pages"
      @previous="page = Math.max(0, page - 1)"
      @next="page = Math.min(Math.ceil(blocks.length / HARNESS_PAGE_SIZE) - 1, page + 1)"
    />
    <template v-for="entry in visibleEntries" :key="blockKey(entry.block, entry.index)">
      <div
        v-if="entry.block.kind === 'text'"
        :data-block-kind="'text'"
        :data-part-id="entry.block.part.id"
      >
        <HarnessMarkdown :text="entry.block.part.output" />
        <span
          v-if="showStreamingCursor && entry.index === blocks.length - 1"
          class="ml-0.5 inline-block h-4 w-2 animate-pulse bg-primary/60 align-middle"
        />
      </div>
      <div
        v-else-if="entry.block.kind === 'single'"
        :data-block-kind="'single'"
        :data-part-id="entry.block.part.id"
      >
        <HarnessWorkRow :part="entry.block.part" />
      </div>
      <div v-else-if="entry.block.kind === 'group'" :data-block-kind="'group'">
        <HarnessWorkedGroup :parts="entry.block.parts" />
      </div>
      <div
        v-else-if="entry.block.kind === 'card'"
        :data-block-kind="'card'"
        :data-part-id="entry.block.part.id"
      >
        <HarnessSubtaskCard
          v-if="entry.block.part.type === 'subtask'"
          :part="entry.block.part"
          :child-session-id="childIdFor(entry.block.part)"
          :models="models"
          @open-subtask="emit('openSubtask', $event)"
        />
        <HarnessPatchCard v-else-if="entry.block.part.type === 'patch'" :part="entry.block.part" />
        <HarnessQuestionCard
          v-else-if="entry.block.part.type === 'tool' && isQuestionToolPart(entry.block.part)"
          :part="entry.block.part"
        />
        <div
          v-else
          class="w-full overflow-x-auto rounded-xl border border-border bg-card px-3 py-2"
        >
          <p class="text-xs font-medium text-muted-foreground">
            {{ entry.block.part.title || entry.block.part.type }}
          </p>
          <pre
            class="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-muted-foreground"
            >{{ entry.block.part.output }}</pre
          >
        </div>
      </div>
      <div
        v-else-if="entry.block.kind === 'compaction'"
        :data-block-kind="'compaction'"
        :data-part-id="entry.block.part.id"
        class="py-1"
      >
        <HarnessCompactionRow :part="entry.block.part" />
      </div>
    </template>
  </div>
</template>
