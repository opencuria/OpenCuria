<script setup lang="ts">
import { ref, watch } from 'vue'
import HarnessContextSheet from '@/components/chat/HarnessContextSheet.vue'
import HarnessMentionSheet from '@/components/chat/HarnessMentionSheet.vue'
import HarnessNoticeSheet from '@/components/chat/HarnessNoticeSheet.vue'
import HarnessPermissionSheet from '@/components/chat/HarnessPermissionSheet.vue'
import HarnessProcessSheet from '@/components/chat/HarnessProcessSheet.vue'
import HarnessQuestionSheet from '@/components/chat/HarnessQuestionSheet.vue'
import HarnessTodoSheet from '@/components/chat/HarnessTodoSheet.vue'
import type { MentionCandidate } from '@/lib/harnessMentions'
import type { ComposerSheet } from '@/lib/composerSheets'
import type { HarnessPermissionResponse } from '@/types/harness'

const props = defineProps<{
  sheets: ComposerSheet[]
  questionSubmitting?: boolean
  permissionResolving?: boolean
}>()

const emit = defineEmits<{
  'mention-select': [candidate: MentionCandidate]
  'mention-hover': [index: number]
  'question-submit': [requestId: string, answers: string[]]
  'question-skip': [requestId: string]
  resolve: [requestId: string, response: HarnessPermissionResponse]
  'close-context': []
  'close-processes': []
  'dismiss-notice': [messageId: string]
}>()

const todoOpen = ref(true)

watch(
  () => props.sheets,
  (sheets) => {
    if (sheets.some((sheet) => sheet.kind === 'todos')) return
    todoOpen.value = true
  },
)

function peekOffset(index: number): number {
  return (index + 1) * 10
}
</script>

<template>
  <div
    v-if="sheets.length > 0"
    class="-mb-px mx-6 shrink-0 sm:mx-7"
    data-testid="composer-sheet-stack"
  >
    <div class="relative">
      <!-- Peek edges of the lower sheets (iOS sheet-stack style). -->
      <div
        v-for="(sheet, index) in sheets.slice(1)"
        :key="`${sheet.kind}-${index}`"
        aria-hidden="true"
        data-testid="composer-sheet-peek"
        class="pointer-events-none absolute inset-x-3 rounded-t-xl border border-b-0 border-border bg-card"
        :style="{ top: `-${peekOffset(index)}px`, height: `${peekOffset(index) + 10}px` }"
      />
      <!-- Topmost sheet: the only interactive one. -->
      <div
        class="relative rounded-t-xl border border-b-0 border-border bg-card shadow-lg"
        data-testid="composer-sheet-top"
        :data-sheet-kind="sheets[0]?.kind"
      >
        <template v-if="sheets[0]?.kind === 'mention' && sheets[0]?.mention">
          <HarnessMentionSheet
            :candidates="sheets[0].mention!.candidates"
            :active-index="sheets[0].mention!.activeIndex"
            @select="emit('mention-select', $event)"
            @hover="emit('mention-hover', $event)"
          />
        </template>
        <template v-else-if="sheets[0]?.kind === 'question' && sheets[0]?.questions">
          <HarnessQuestionSheet
            :requests="sheets[0].questions!"
            :submitting="questionSubmitting"
            @submit="(requestId, answers) => emit('question-submit', requestId, answers)"
            @skip="(requestId) => emit('question-skip', requestId)"
          />
        </template>
        <template v-else-if="sheets[0]?.kind === 'permission' && sheets[0]?.permissions">
          <HarnessPermissionSheet
            :requests="sheets[0].permissions!"
            :resolving="permissionResolving"
            @resolve="(requestId, response) => emit('resolve', requestId, response)"
          />
        </template>
        <template v-else-if="sheets[0]?.kind === 'notice' && sheets[0]?.notice">
          <HarnessNoticeSheet
            :notice="sheets[0].notice!"
            @dismiss="(messageId) => emit('dismiss-notice', messageId)"
          />
        </template>
        <template v-else-if="sheets[0]?.kind === 'processes'">
          <HarnessProcessSheet @close="emit('close-processes')" />
        </template>
        <template v-else-if="sheets[0]?.kind === 'context' && sheets[0]?.context">
          <HarnessContextSheet :context="sheets[0].context!" @close="emit('close-context')" />
        </template>
        <template v-else-if="sheets[0]?.kind === 'todos' && sheets[0]?.todos">
          <HarnessTodoSheet v-model:open="todoOpen" :todos="sheets[0].todos!" />
        </template>
      </div>
    </div>
  </div>
</template>
