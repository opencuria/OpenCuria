<script setup lang="ts">
import { computed } from 'vue'

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments } from '@/lib/toolDisplay'

const props = defineProps<{
  part: HarnessPart
}>()

interface QuestionRow {
  question: string
  answer: string
}

function asQuestionList(value: unknown): { question: string }[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const question = (item as { question?: unknown }).question
    if (typeof question !== 'string' || !question) return []
    return [{ question }]
  })
}

function asAnswerList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
}

const rows = computed<QuestionRow[]>(() => {
  const args = parseToolArguments(props.part)
  const questions = asQuestionList(props.part.meta?.['questions'] ?? args.questions)
  let answers = asAnswerList(props.part.meta?.['answers'])
  if (answers.length === 0 && props.part.output) {
    try {
      const parsed: unknown = JSON.parse(props.part.output)
      if (parsed && typeof parsed === 'object' && 'answers' in parsed) {
        answers = asAnswerList((parsed as { answers: unknown }).answers)
      }
    } catch {
      answers = []
    }
  }
  if (questions.length === 0 && answers.length === 0) return []
  if (questions.length === 0) {
    return answers.map((answer) => ({ question: 'Answer', answer }))
  }
  return questions.map((item, index) => ({
    question: item.question,
    answer: answers[index] ?? '',
  }))
})
</script>

<template>
  <div data-testid="tool-detail-question" class="min-w-0 space-y-2">
    <div
      v-for="(row, index) in rows"
      :key="index"
      class="rounded-md bg-muted/50 px-2 py-1.5"
    >
      <p class="text-[11px] font-medium text-foreground">{{ row.question }}</p>
      <p
        v-if="row.answer"
        data-testid="tool-detail-question-answer"
        class="mt-0.5 text-[11px] text-muted-foreground"
      >
        {{ row.answer }}
      </p>
      <p v-else-if="part.state === 'running'" class="mt-0.5 text-[11px] text-muted-foreground">
        Waiting for answer…
      </p>
    </div>
    <p v-if="!rows.length && part.output" class="text-[11px] text-muted-foreground">
      {{ part.output }}
    </p>
  </div>
</template>
