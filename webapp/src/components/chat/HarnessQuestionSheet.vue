<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronDown, ChevronUp, MessageCircleQuestion } from '@lucide/vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { optionLetter } from '@/lib/composerSheets'
import { gateSourceLabel } from '@/lib/harnessSubtaskActivity'
import type { HarnessQuestionRequest } from '@/types/harness'

/**
 * Separator for multi-select answers. Must match the backend contract and
 * the previous inline form (NUL-joined labels).
 */
const MULTI_SELECT_SEPARATOR = String.fromCharCode(0)

const props = defineProps<{
  requests: HarnessQuestionRequest[]
  submitting?: boolean
}>()

const emit = defineEmits<{
  submit: [requestId: string, answers: string[]]
  skip: [requestId: string]
}>()

const page = ref(0)
/**
 * Raw answers per request id, one entry per question. Option selections are
 * stored separator-joined (multi-select) or as a single label.
 */
const answersByRequest = ref<Record<string, string[]>>({})

const total = computed(() => props.requests.length)
const request = computed<HarnessQuestionRequest | null>(
  () => props.requests[Math.min(page.value, Math.max(total.value - 1, 0))] ?? null,
)

const sourceLabel = computed(() => gateSourceLabel(request.value?.agent_name))

function answersFor(requestId: string, count: number): string[] {
  let answers = answersByRequest.value[requestId]
  if (!answers) {
    answers = Array.from({ length: count }, () => '')
    answersByRequest.value[requestId] = answers
  }
  return answers
}

watch(
  () => props.requests,
  (requests) => {
    if (page.value > requests.length - 1) {
      page.value = Math.max(0, requests.length - 1)
    }
    for (const item of requests) {
      answersFor(item.request_id, item.questions.length)
    }
    for (const id of Object.keys(answersByRequest.value)) {
      if (!requests.some((item) => item.request_id === id)) {
        delete answersByRequest.value[id]
      }
    }
  },
  { immediate: true },
)

function selectedOptions(requestId: string, questionIndex: number): string[] {
  const raw = answersByRequest.value[requestId]?.[questionIndex] ?? ''
  return raw ? raw.split(MULTI_SELECT_SEPARATOR).filter(Boolean) : []
}

function setAnswer(requestId: string, questionIndex: number, value: string): void {
  const answers = answersByRequest.value[requestId]
  if (!answers) return
  answers[questionIndex] = value
}

function toggleOption(
  requestId: string,
  questionIndex: number,
  label: string,
  multiple: boolean,
): void {
  if (multiple) {
    const current = selectedOptions(requestId, questionIndex)
    const next = current.includes(label)
      ? current.filter((item) => item !== label)
      : [...current, label]
    setAnswer(requestId, questionIndex, next.join(MULTI_SELECT_SEPARATOR))
  } else {
    setAnswer(requestId, questionIndex, label)
  }
}

function isOptionSelected(requestId: string, questionIndex: number, label: string): boolean {
  return selectedOptions(requestId, questionIndex).includes(label)
}

function collectAnswers(item: HarnessQuestionRequest): string[] {
  const raw = answersByRequest.value[item.request_id] ?? []
  return item.questions.map((question, index) => {
    if (question.options?.length) {
      const selected = (raw[index] ?? '').split(MULTI_SELECT_SEPARATOR).filter(Boolean)
      if (question.multiple) return selected.join(MULTI_SELECT_SEPARATOR)
      return selected[0] ?? ''
    }
    return (raw[index] ?? '').trim()
  })
}

function handleSubmit(): void {
  if (!request.value || props.submitting) return
  emit('submit', request.value.request_id, collectAnswers(request.value))
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
    toggleOption(item.request_id, questionIndex, option.label, question.multiple ?? false)
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
    <div class="flex items-center gap-2 px-4 pt-3">
      <MessageCircleQuestion :size="14" class="shrink-0 text-muted-foreground" />
      <p class="text-sm font-medium text-foreground">Questions</p>
      <Badge
        v-if="sourceLabel"
        variant="outline"
        data-testid="composer-question-source"
      >
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

    <div
      class="flex flex-col gap-4 overflow-y-auto px-4 pb-1 pt-2"
      :class="request.questions.length > 2 ? 'max-h-72' : ''"
    >
      <div
        v-for="(question, qIndex) in request.questions"
        :key="`${request.request_id}-${qIndex}`"
        class="space-y-2"
      >
        <p
          v-if="question.header"
          class="text-xs font-semibold uppercase tracking-wide text-muted-foreground"
        >
          {{ question.header }}
        </p>
        <p class="text-sm text-foreground">
          <span class="mr-1.5 font-semibold tabular-nums">{{ qIndex + 1 }}.</span>
          <span class="font-semibold">{{ question.question }}</span>
        </p>
        <div v-if="question.options?.length" class="flex flex-col gap-1.5">
          <Button
            v-for="(option, oIndex) in question.options"
            :key="option.label"
            type="button"
            size="sm"
            :variant="
              isOptionSelected(request.request_id, qIndex, option.label) ? 'default' : 'outline'
            "
            class="h-auto justify-start whitespace-normal text-left"
            data-testid="composer-question-option"
            @click="
              toggleOption(request.request_id, qIndex, option.label, question.multiple ?? false)
            "
          >
            <span
              class="mr-1 flex h-5 w-5 shrink-0 items-center justify-center rounded text-[11px] font-semibold"
              :class="
                isOptionSelected(request.request_id, qIndex, option.label)
                  ? 'bg-primary-foreground/20 text-primary-foreground'
                  : 'bg-muted text-muted-foreground'
              "
            >
              {{ optionLetter(oIndex) }}
            </span>
            <span class="font-medium">{{ option.label }}</span>
            <span v-if="option.description" class="ml-1 text-xs font-normal opacity-70">
              — {{ option.description }}
            </span>
          </Button>
        </div>
        <div v-else class="space-y-1">
          <Label :for="`composer-question-${request.request_id}-${qIndex}`">Your answer</Label>
          <Input
            :id="`composer-question-${request.request_id}-${qIndex}`"
            :model-value="answersByRequest[request.request_id]?.[qIndex] ?? ''"
            :disabled="submitting"
            placeholder="Type your answer"
            @update:model-value="setAnswer(request.request_id, qIndex, String($event ?? ''))"
          />
        </div>
      </div>
    </div>

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
