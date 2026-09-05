<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { MessageCircleQuestion } from '@lucide/vue'
import type { HarnessQuestionRequest } from '@/types/harness'

const props = defineProps<{
  request: HarnessQuestionRequest | null
  submitting?: boolean
}>()

const emit = defineEmits<{
  submit: [answers: string[]]
  reject: []
}>()

const freeTextAnswers = ref<string[]>([])

watch(
  () => props.request,
  (request) => {
    if (!request) {
      freeTextAnswers.value = []
      return
    }
    freeTextAnswers.value = request.questions.map(() => '')
  },
  { immediate: true },
)

const visible = computed(() => props.request !== null)

function selectedOptions(questionIndex: number): string[] {
  const request = props.request
  if (!request) return []
  const question = request.questions[questionIndex]
  if (!question?.options?.length) return []
  return freeTextAnswers.value[questionIndex]
    ? freeTextAnswers.value[questionIndex].split('\u0000').filter(Boolean)
    : []
}

function toggleOption(questionIndex: number, label: string, multiple: boolean): void {
  const current = selectedOptions(questionIndex)
  if (multiple) {
    const next = current.includes(label)
      ? current.filter((item) => item !== label)
      : [...current, label]
    freeTextAnswers.value[questionIndex] = next.join('\u0000')
  } else {
    freeTextAnswers.value[questionIndex] = label
  }
}

function isOptionSelected(questionIndex: number, label: string): boolean {
  return selectedOptions(questionIndex).includes(label)
}

function handleSubmit(): void {
  if (!props.request) return
  const answers = props.request.questions.map((question, index) => {
    if (question.options?.length) {
      const selected = selectedOptions(index)
      if (question.multiple) return selected
      return selected[0] ?? ''
    }
    return freeTextAnswers.value[index]?.trim() ?? ''
  })
  emit('submit', answers)
}
</script>

<template>
  <div
    v-if="visible && request"
    class="mx-4 mb-3 rounded-xl border border-primary/30 bg-card p-4 shadow-sm"
  >
    <div class="mb-3 flex items-center gap-2">
      <MessageCircleQuestion :size="16" class="text-primary" />
      <p class="text-sm font-medium text-foreground">Agent question</p>
      <Badge variant="secondary">Waiting for you</Badge>
    </div>
    <div class="flex flex-col gap-4">
      <div
        v-for="(question, qIndex) in request.questions"
        :key="`${request.request_id}-${qIndex}`"
        class="space-y-2"
      >
        <p v-if="question.header" class="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {{ question.header }}
        </p>
        <p class="text-sm text-foreground">{{ question.question }}</p>
        <div v-if="question.options?.length" class="flex flex-col gap-2">
          <Button
            v-for="option in question.options"
            :key="option.label"
            type="button"
            size="sm"
            :variant="isOptionSelected(qIndex, option.label) ? 'default' : 'outline'"
            class="h-auto justify-start whitespace-normal text-left"
            @click="toggleOption(qIndex, option.label, question.multiple)"
          >
            <span class="font-medium">{{ option.label }}</span>
            <span v-if="option.description" class="ml-1 text-xs opacity-80">
              — {{ option.description }}
            </span>
          </Button>
        </div>
        <div v-else class="space-y-1">
          <Label :for="`question-${qIndex}`">Your answer</Label>
          <Input
            :id="`question-${qIndex}`"
            v-model="freeTextAnswers[qIndex]"
            :disabled="submitting"
            placeholder="Type your answer"
          />
        </div>
      </div>
    </div>
    <div class="mt-4 flex flex-wrap justify-end gap-2">
      <Button variant="outline" size="sm" :disabled="submitting" @click="emit('reject')">
        Dismiss
      </Button>
      <Button size="sm" :disabled="submitting" @click="handleSubmit">
        Submit answers
      </Button>
    </div>
  </div>
</template>
