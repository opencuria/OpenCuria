<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronDown, ChevronUp, MessageCircleQuestion, PenLine } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { optionLetter } from '@opencuria/chat-core/composerSheets'
import { gateSourceLabel } from '@opencuria/chat-core/harnessSubtaskActivity'
import type { HarnessQuestionRequest } from '@opencuria/chat-core/harness'
import {
  collectQuestionAnswers,
  emptyQuestionDraft,
  isQuestionCustomActive,
  selectedQuestionOptions,
  setQuestionCustomAnswer,
  toggleQuestionOption,
  type QuestionDraft,
} from '@opencuria/chat-core/harnessQuestion'
import { useAuthStore } from '@/stores/auth'
import {
  loadQuestionInputCache,
  questionInputCacheKey,
  saveQuestionInputCache,
} from '@/lib/questionInputCache'

const props = defineProps<{
  requests: HarnessQuestionRequest[]
  submitting?: boolean
}>()

const emit = defineEmits<{
  submit: [requestId: string, answers: string[]]
  skip: [requestId: string]
}>()

const auth = useAuthStore()
const page = ref(0)
const cacheKeys: Record<string, string> = {}
/** Unsent answers per request id (shared chat-core draft rules). */
const drafts = ref<Record<string, QuestionDraft>>({})

const total = computed(() => props.requests.length)
const request = computed<HarnessQuestionRequest | null>(
  () => props.requests[Math.min(page.value, Math.max(total.value - 1, 0))] ?? null,
)

const sourceLabel = computed(() => gateSourceLabel(request.value?.agent_name))

watch(
  [() => props.requests, () => auth.user?.id, () => auth.activeOrganizationId],
  ([requests, userId, organizationId]) => {
    if (page.value > requests.length - 1) {
      page.value = Math.max(0, requests.length - 1)
    }
    for (const item of requests) {
      const id = item.request_id
      const key = questionInputCacheKey({ userId, organizationId }, item)
      if (cacheKeys[id] === key && drafts.value[id]) continue
      drafts.value[id] =
        loadQuestionInputCache(key, item.questions.length) ?? emptyQuestionDraft(item)
      cacheKeys[id] = key
    }
    // Leaving a chat is not a resolution: prune only local state, not the cache.
    for (const id of Object.keys(cacheKeys)) {
      if (requests.some((item) => item.request_id === id)) continue
      delete cacheKeys[id]
      delete drafts.value[id]
    }
  },
  { immediate: true, flush: 'sync' },
)

function draftFor(item: HarnessQuestionRequest): QuestionDraft {
  return drafts.value[item.request_id] ?? emptyQuestionDraft(item)
}

function updateDraft(item: HarnessQuestionRequest, draft: QuestionDraft): void {
  drafts.value[item.request_id] = draft
  saveQuestionInputCache(cacheKeys[item.request_id] ?? '', draft)
}

function setCustom(item: HarnessQuestionRequest, questionIndex: number, value: string): void {
  updateDraft(item, setQuestionCustomAnswer(draftFor(item), item, questionIndex, value))
}

function toggleOption(item: HarnessQuestionRequest, questionIndex: number, label: string): void {
  updateDraft(item, toggleQuestionOption(draftFor(item), item, questionIndex, label))
}

function isOptionSelected(
  item: HarnessQuestionRequest,
  questionIndex: number,
  label: string,
): boolean {
  return selectedQuestionOptions(draftFor(item), questionIndex).includes(label)
}

function isCustomActive(item: HarnessQuestionRequest, questionIndex: number): boolean {
  return isQuestionCustomActive(draftFor(item), item, questionIndex)
}

function handleSubmit(): void {
  if (!request.value || props.submitting) return
  emit(
    'submit',
    request.value.request_id,
    collectQuestionAnswers(request.value, draftFor(request.value)),
  )
}

function handleSkip(): void {
  if (!request.value || props.submitting) return
  emit('skip', request.value.request_id)
}

/**
 * Letter shortcuts select within the first question that offers options;
 * `Escape` skips the request. `Enter` continues unless focus is inside a
 * free-text field or on a button (native behavior applies there).
 */
function onKeydown(event: KeyboardEvent): void {
  const item = request.value
  if (!item || props.submitting) return
  if (event.key === 'Escape') {
    event.preventDefault()
    handleSkip()
    return
  }
  const target = event.target as HTMLElement | null
  const tag = target?.tagName ?? ''
  if (/^[a-zA-Z]$/.test(event.key) && tag !== 'INPUT' && tag !== 'TEXTAREA') {
    const questionIndex = item.questions.findIndex((question) => question.options?.length)
    if (questionIndex === -1) return
    const question = item.questions[questionIndex]!
    const optionIndex = event.key.toUpperCase().charCodeAt(0) - 65
    const option = question.options?.[optionIndex]
    if (!option) return
    event.preventDefault()
    toggleOption(item, questionIndex, option.label)
    return
  }
  if (event.key === 'Enter' && tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'BUTTON') {
    event.preventDefault()
    handleSubmit()
  }
}
</script>

<template>
  <div v-if="request" data-testid="composer-question-sheet" @keydown="onKeydown">
    <!-- Header bar -->
    <div class="flex items-center gap-2 px-4 pt-3">
      <MessageCircleQuestion :size="14" class="shrink-0 text-muted-foreground" />
      <p class="text-sm font-medium text-foreground">Questions</p>
      <Badge v-if="sourceLabel" variant="outline" data-testid="composer-question-source">
        {{ sourceLabel }}
      </Badge>
      <div class="ml-auto flex items-center gap-0.5" data-testid="composer-question-pager">
        <Button
          variant="ghost"
          size="icon-xs"
          :disabled="page <= 0 || submitting"
          title="Previous question"
          data-testid="composer-question-prev"
          @click="page = Math.max(0, page - 1)"
        >
          <ChevronUp :size="14" />
        </Button>
        <span class="min-w-10 text-center text-xs tabular-nums text-muted-foreground">
          {{ page + 1 }} of {{ total }}
        </span>
        <Button
          variant="ghost"
          size="icon-xs"
          :disabled="page >= total - 1 || submitting"
          title="Next question"
          data-testid="composer-question-next"
          @click="page = Math.min(total - 1, page + 1)"
        >
          <ChevronDown :size="14" />
        </Button>
      </div>
    </div>

    <!-- Question body -->
    <div
      class="flex flex-col gap-4 overflow-y-auto px-4 pb-1 pt-2"
      :class="request.questions.length > 2 ? 'max-h-72' : ''"
    >
      <div
        v-for="(question, qIndex) in request.questions"
        :key="`${request.request_id}-${qIndex}`"
        class="space-y-2"
      >
        <!-- Optional header label -->
        <p
          v-if="question.header"
          class="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"
        >
          {{ question.header }}
        </p>

        <!-- Question text — hide number prefix for single-question requests -->
        <p class="text-sm font-medium text-foreground">
          <span
            v-if="request.questions.length > 1"
            class="mr-1.5 tabular-nums text-muted-foreground"
          >
            {{ qIndex + 1 }}.
          </span>
          {{ question.question }}
        </p>

        <!-- Options list (when the question has selectable options) -->
        <div v-if="question.options?.length" class="flex flex-col gap-1">
          <!-- Selection hint: single vs multi choice -->
          <p class="text-xs text-muted-foreground" data-testid="composer-question-hint">
            {{ question.multiple ? 'Select all that apply' : 'Select one' }}
          </p>
          <!-- Selectable option rows -->
          <button
            v-for="(option, oIndex) in question.options"
            :key="option.label"
            type="button"
            :disabled="submitting"
            :role="question.multiple ? 'checkbox' : 'radio'"
            :aria-checked="isOptionSelected(request, qIndex, option.label)"
            class="flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left text-sm transition-colors"
            :class="
              isOptionSelected(request, qIndex, option.label)
                ? 'border-primary bg-accent text-foreground'
                : 'border-border bg-muted/30 text-foreground hover:bg-muted/60'
            "
            :data-variant="isOptionSelected(request, qIndex, option.label) ? 'default' : 'outline'"
            data-testid="composer-question-option"
            @click="toggleOption(request, qIndex, option.label)"
          >
            <span
              class="flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[11px] font-semibold"
              :data-type="question.multiple ? 'checkbox' : 'radio'"
              :data-picked="isOptionSelected(request, qIndex, option.label)"
              :class="
                isOptionSelected(request, qIndex, option.label)
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-muted text-muted-foreground'
              "
            >
              {{ optionLetter(oIndex) }}
            </span>
            <span class="min-w-0 flex-1">
              <span class="font-medium">{{ option.label }}</span>
              <span v-if="option.description" class="ml-1.5 text-xs text-muted-foreground">
                {{ option.description }}
              </span>
            </span>
          </button>

          <!-- Own answer row — styled as the last option in the list -->
          <div
            class="flex items-center gap-2.5 rounded-lg border px-3 py-2 transition-colors"
            :class="
              isCustomActive(request, qIndex)
                ? 'border-primary bg-accent'
                : 'border-border bg-muted/30'
            "
            data-testid="composer-question-custom-row"
          >
            <span
              class="flex h-5 w-5 shrink-0 items-center justify-center rounded-md"
              :class="
                isCustomActive(request, qIndex)
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-muted text-muted-foreground'
              "
            >
              <PenLine :size="11" />
            </span>
            <input
              :id="`composer-question-${request.request_id}-${qIndex}`"
              :value="draftFor(request).custom[qIndex] ?? ''"
              :disabled="submitting"
              placeholder="Own answer…"
              class="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none placeholder:text-muted-foreground"
              data-testid="composer-question-custom"
              @input="
                setCustom(request, qIndex, String(($event.target as HTMLInputElement).value ?? ''))
              "
            />
          </div>
        </div>

        <!-- Free-text only (no selectable options) -->
        <div v-else>
          <input
            :id="`composer-question-${request.request_id}-${qIndex}`"
            :value="draftFor(request).custom[qIndex] ?? ''"
            :disabled="submitting"
            placeholder="Your answer…"
            class="w-full rounded-lg border border-border bg-muted/30 px-3 py-2 text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground focus:border-primary focus:ring-1 focus:ring-primary/30"
            data-testid="composer-question-custom"
            @input="
              setCustom(request, qIndex, String(($event.target as HTMLInputElement).value ?? ''))
            "
          />
        </div>
      </div>
    </div>

    <!-- Footer actions -->
    <div class="flex items-center justify-end gap-2 px-4 pb-3 pt-2">
      <Button
        variant="ghost"
        size="sm"
        :disabled="submitting"
        data-testid="composer-question-skip"
        @click="handleSkip"
      >
        Skip
        <kbd
          class="rounded border border-border bg-muted px-1 font-mono text-[10px] text-muted-foreground"
          >Esc</kbd
        >
      </Button>
      <Button
        size="sm"
        :disabled="submitting"
        data-testid="composer-question-submit"
        @click="handleSubmit"
      >
        Continue
      </Button>
    </div>
  </div>
</template>
