<script setup lang="ts">
/**
 * SendLaterMenu — "Send later" for a draft or a scheduled message.
 *
 * Opens from its trigger (default slot) with the shared presets, computed
 * when the menu opens, and "Choose date & time…", which asks in
 * SendLaterDialog. Emits the chosen time.
 */
import { ref, watch } from 'vue'
import { sendLaterOptions, type SendLaterOption } from '@opencuria/chat-core/sendLater'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import SendLaterDialog from '@/components/chat/SendLaterDialog.vue'

withDefaults(
  defineProps<{
    disabled?: boolean
    align?: 'start' | 'center' | 'end'
    /** Current time of a scheduled message, preselected in the dialog. */
    initial?: Date | string | null
    /** Primary action of the dialog, e.g. "Reschedule". */
    actionLabel?: string
  }>(),
  { disabled: false, align: 'end', initial: null, actionLabel: 'Schedule' },
)

const emit = defineEmits<{ pick: [at: Date] }>()

const open = ref(false)
const dialogOpen = ref(false)
const options = ref<SendLaterOption[]>(sendLaterOptions())

watch(open, (value) => {
  if (value) options.value = sendLaterOptions()
})
</script>

<template>
  <DropdownMenu v-model:open="open">
    <DropdownMenuTrigger as-child :disabled="disabled">
      <slot />
    </DropdownMenuTrigger>
    <DropdownMenuContent side="top" :align="align" class="w-56" data-testid="send-later-menu">
      <DropdownMenuLabel>Send later</DropdownMenuLabel>
      <DropdownMenuItem
        v-for="option in options"
        :key="option.id"
        :data-testid="`send-later-${option.id}`"
        @click="emit('pick', option.at)"
      >
        {{ option.label }}
        <span class="ml-auto text-xs tabular-nums text-muted-foreground">{{ option.detail }}</span>
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem data-testid="send-later-custom" @click="dialogOpen = true">
        Choose date &amp; time…
      </DropdownMenuItem>
    </DropdownMenuContent>
  </DropdownMenu>
  <SendLaterDialog
    v-model:open="dialogOpen"
    :initial="initial"
    :action-label="actionLabel"
    @confirm="emit('pick', $event)"
  />
</template>
