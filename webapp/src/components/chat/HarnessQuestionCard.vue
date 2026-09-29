<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useHarnessPartDetail } from '@/lib/harnessPartDetail'
import { MessageCircleQuestion } from '@lucide/vue'
import type { HarnessPart } from '@/types/harness'
import { parseQuestionRows, questionCardStatus } from '@/lib/harnessQuestion'

const props = defineProps<{ part: HarnessPart }>()
const open = ref(false)
const partRef = computed(() => props.part)
const {
  loading: detailLoading,
  error: detailError,
  load: loadDetail,
  setExpanded,
} = useHarnessPartDetail(partRef)
watch(open, (expanded) => {
  setExpanded(expanded)
  if (expanded) void loadDetail()
})
watch(
  () => props.part.state,
  (state) => {
    if (open.value && (state === 'completed' || state === 'error')) void loadDetail()
  },
)
const rows = computed(() =>
  props.part.detail_loaded === false && props.part.display?.question_rows?.length
    ? props.part.display.question_rows.map((row) => ({
        question: row.question,
        answer: row.answer ?? '',
      }))
    : parseQuestionRows(props.part),
)
const status = computed(() => {
  if (props.part.state === 'error' && props.part.display && props.part.output === 'Tool failed') {
    return { tone: 'failed' as const, label: 'Failed', detail: null }
  }
  return questionCardStatus(props.part)
})
const countLabel = computed(() => (rows.value.length > 1 ? `${rows.value.length} questions` : null))
</script>

<template>
  <div
    data-testid="harness-question-card"
    :data-part-id="part.id"
    class="w-full overflow-hidden rounded-xl border border-border bg-card"
  >
    <div class="flex min-w-0 items-center gap-1.5 px-3 py-1.5 text-xs">
      <MessageCircleQuestion :size="12" class="shrink-0 text-muted-foreground" />
      <span class="min-w-0 truncate font-medium text-foreground">Question</span>
      <span v-if="countLabel" class="shrink-0 tabular-nums text-muted-foreground">{{
        countLabel
      }}</span>
      <span
        v-if="status.label"
        data-testid="harness-question-card-status"
        class="ml-auto shrink-0 font-medium"
        :class="status.tone === 'failed' ? 'text-destructive' : 'text-muted-foreground'"
        >{{ status.label }}</span
      >
    </div>
    <div v-if="rows.length" class="space-y-1.5 border-t border-border px-3 py-2">
      <div v-for="(row, index) in rows" :key="index" class="min-w-0">
        <p class="break-words text-[13px] font-medium text-foreground">
          <span v-if="rows.length > 1" class="mr-1.5 tabular-nums text-muted-foreground"
            >{{ index + 1 }}.</span
          >{{ row.question }}
        </p>
        <p
          v-if="row.answer"
          data-testid="harness-question-card-answer"
          class="mt-0.5 break-words text-[13px] text-muted-foreground"
        >
          {{ row.answer }}
        </p>
        <p v-else class="mt-0.5 text-[13px] italic text-muted-foreground/70">No answer</p>
      </div>
    </div>
    <button
      type="button"
      class="w-full border-t border-border px-3 py-1 text-left text-xs text-muted-foreground hover:text-foreground"
      @click="open = !open"
    >
      {{ open ? 'Hide question details' : 'Show question details' }}
    </button>
    <div v-if="open" class="border-t border-border px-3 py-2">
      <p
        v-if="detailLoading"
        data-testid="harness-part-detail-loading"
        class="text-xs text-muted-foreground"
      >
        Loading details…
      </p>
      <div v-else-if="detailError" class="text-xs text-destructive">
        <p>{{ detailError }}</p>
        <button type="button" class="mt-1 underline" @click="loadDetail">Retry</button>
      </div>
      <p
        v-else-if="!rows.length && status.detail"
        class="break-words text-[13px] text-muted-foreground"
      >
        {{ status.detail }}
      </p>
      <p
        v-else-if="!rows.length && part.output"
        class="break-words text-[13px] text-muted-foreground"
      >
        {{ part.output }}
      </p>
    </div>
  </div>
</template>
