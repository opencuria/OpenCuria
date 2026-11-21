<script setup lang="ts">
/**
 * SendLaterDialog — a custom date and time for "Send later".
 *
 * Starts from the given time while it is still ahead (rescheduling), else
 * from the next full hour, and validates with the shared rules.
 */
import { computed, ref, useId, watch } from 'vue'
import {
  SEND_LATER_MAX_DAYS,
  sendLaterDefault,
  sendLaterError,
  sendLaterFields,
  sendLaterFromFields,
} from '@opencuria/chat-core/sendLater'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'

const props = withDefaults(
  defineProps<{
    open: boolean
    /** Preselected time, e.g. the current time of a scheduled message. */
    initial?: Date | string | null
    /** Primary action, e.g. "Schedule" or "Reschedule". */
    actionLabel?: string
  }>(),
  { initial: null, actionLabel: 'Schedule' },
)

const emit = defineEmits<{
  'update:open': [value: boolean]
  confirm: [at: Date]
}>()

const DAY_MS = 24 * 60 * 60 * 1000
const formId = `send-later-${useId()}`
const date = ref('')
const time = ref('')
/** Validation reference time: refreshed on every edit and on confirm. */
const now = ref(new Date())

watch(
  () => props.open,
  (open) => {
    if (!open) return
    const initial = props.initial ? new Date(props.initial) : null
    const start = initial && !sendLaterError(initial) ? initial : sendLaterDefault()
    ;({ date: date.value, time: time.value } = sendLaterFields(start))
  },
  { immediate: true },
)

watch([date, time], () => {
  now.value = new Date()
})

const at = computed(() => sendLaterFromFields(date.value, time.value))
const error = computed(() => sendLaterError(at.value, now.value))
const minDate = computed(() => sendLaterFields(now.value).date)
const maxDate = computed(
  () => sendLaterFields(new Date(now.value.getTime() + SEND_LATER_MAX_DAYS * DAY_MS)).date,
)

function confirm(): void {
  now.value = new Date()
  if (error.value || !at.value) return
  emit('confirm', at.value)
  emit('update:open', false)
}
</script>

<template>
  <Dialog :open="open" @update:open="emit('update:open', $event)">
    <DialogContent class="sm:max-w-sm" data-testid="send-later-dialog">
      <DialogHeader>
        <DialogTitle>Send later</DialogTitle>
        <DialogDescription>Choose when to send the message.</DialogDescription>
      </DialogHeader>
      <form :id="formId" class="grid grid-cols-2 gap-3" novalidate @submit.prevent="confirm">
        <label class="block min-w-0 space-y-1.5">
          <span class="block text-xs font-medium">Date</span>
          <Input
            v-model="date"
            type="date"
            :min="minDate"
            :max="maxDate"
            class="h-9 rounded-lg"
            :aria-invalid="error ? true : undefined"
            :aria-describedby="error ? `${formId}-error` : undefined"
            data-testid="send-later-date"
          />
        </label>
        <label class="block min-w-0 space-y-1.5">
          <span class="block text-xs font-medium">Time</span>
          <Input
            v-model="time"
            type="time"
            class="h-9 rounded-lg"
            :aria-invalid="error ? true : undefined"
            :aria-describedby="error ? `${formId}-error` : undefined"
            data-testid="send-later-time"
          />
        </label>
        <p
          v-if="error"
          :id="`${formId}-error`"
          class="col-span-2 text-xs text-destructive"
          role="alert"
          data-testid="send-later-error"
        >
          {{ error }}
        </p>
      </form>
      <DialogFooter>
        <Button
          variant="outline"
          data-testid="send-later-cancel"
          @click="emit('update:open', false)"
        >
          Cancel
        </Button>
        <Button
          type="submit"
          :form="formId"
          :disabled="Boolean(error)"
          data-testid="send-later-confirm"
        >
          {{ actionLabel }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
