<script setup lang="ts">
/**
 * One-line conversation row: status slot, truncated title, trailing
 * time/workspace replaced by a hover menu (rename / mark read / unread / delete).
 */
import { computed, nextTick, ref } from 'vue'
import {
  Check,
  CircleAlert,
  Loader2,
  Mail,
  MailOpen,
  MessageCircleQuestion,
  MoreHorizontal,
  Pencil,
  ShieldAlert,
  Trash2,
  X,
} from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { conversationTitle, formatTimeAgo } from '@/lib/conversationGroups'
import type { HarnessAttentionKind, HarnessConversation } from '@/types/harness'

const props = withDefaults(
  defineProps<{
    conversation: HarnessConversation
    active?: boolean
    showWorkspace?: boolean
  }>(),
  { active: false, showWorkspace: false },
)

const emit = defineEmits<{
  select: [conversation: HarnessConversation]
  rename: [conversation: HarnessConversation, title: string]
  delete: [conversation: HarnessConversation]
  'mark-read': [conversation: HarnessConversation]
  'mark-unread': [conversation: HarnessConversation]
}>()

const editing = ref(false)
const editTitle = ref('')
const inputRef = ref<{ $el: HTMLInputElement } | null>(null)

const title = computed(() => conversationTitle(props.conversation))
const needsAttention = computed(() => Boolean(props.conversation.needs_attention))
const attentionKind = computed<HarnessAttentionKind>(
  () => props.conversation.attention_kind ?? '',
)
const attentionIcon = computed(() => {
  if (attentionKind.value === 'question') return MessageCircleQuestion
  if (attentionKind.value === 'permission') return ShieldAlert
  return CircleAlert
})
const attentionLabel = computed(() => {
  if (attentionKind.value === 'question') return 'Question waiting'
  if (attentionKind.value === 'permission') return 'Permission required'
  if (attentionKind.value === 'both') return 'Permission and question waiting'
  return 'Action required'
})

function handleSelect(): void {
  if (editing.value) return
  emit('select', props.conversation)
}

async function startRename(): Promise<void> {
  editing.value = true
  editTitle.value = title.value
  await nextTick()
  inputRef.value?.$el?.focus()
  inputRef.value?.$el?.select()
}

function cancelRename(): void {
  editing.value = false
  editTitle.value = ''
}

function confirmRename(): void {
  const nextTitle = editTitle.value.trim()
  if (!nextTitle) return
  editing.value = false
  emit('rename', props.conversation, nextTitle)
}

function tooltipDate(): string {
  return new Date(props.conversation.updated_at).toLocaleString('de-DE', {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}
</script>

<template>
  <div
    v-if="editing"
    class="flex h-8 items-center gap-1 rounded-xl px-2"
    data-testid="conversation-row-editing"
  >
    <Input
      ref="inputRef"
      v-model="editTitle"
      class="h-7 flex-1 rounded-lg text-xs"
      maxlength="255"
      data-testid="conversation-rename-input"
      aria-label="Chat umbenennen"
      @keydown.enter.prevent="confirmRename"
      @keydown.esc.prevent="cancelRename"
      @click.stop
    />
    <Button
      variant="ghost"
      size="icon-xs"
      aria-label="Umbenennen bestätigen"
      @click.stop="confirmRename"
    >
      <Check />
    </Button>
    <Button
      variant="ghost"
      size="icon-xs"
      aria-label="Umbenennen abbrechen"
      @click.stop="cancelRename"
    >
      <X />
    </Button>
  </div>

  <div
    v-else
    role="button"
    tabindex="0"
    data-testid="conversation-row"
    :aria-selected="props.active"
    :aria-label="needsAttention ? `Chat ${title} öffnen — ${attentionLabel}` : `Chat ${title} öffnen`"
    class="group/row relative flex h-8 cursor-pointer items-center gap-1.5 rounded-xl px-2 text-sm transition-colors focus-visible:outline-2 focus-visible:outline-primary"
    :class="
      needsAttention
        ? props.active
          ? 'bg-amber-500/20'
          : 'bg-amber-500/10 hover:bg-amber-500/15'
        : props.active
          ? 'bg-primary/10'
          : 'hover:bg-muted'
    "
    @click="handleSelect"
    @keydown.enter="handleSelect"
  >
    <span
      v-if="needsAttention"
      class="absolute inset-y-1 left-0 w-0.5 rounded-full bg-amber-500"
      data-testid="attention-edge"
    />
    <div class="flex size-4 shrink-0 items-center justify-center">
      <component
        :is="attentionIcon"
        v-if="needsAttention"
        data-testid="attention-icon"
        class="size-3.5 animate-pulse text-amber-500"
      />
      <Loader2
        v-else-if="props.conversation.status === 'busy'"
        data-testid="busy-spinner"
        class="size-3 animate-spin text-primary"
      />
      <span
        v-else-if="props.conversation.unread"
        data-testid="unread-dot"
        class="size-1.5 rounded-full bg-primary"
      />
    </div>

    <Tooltip :delay-duration="500">
      <TooltipTrigger as-child>
        <span
          class="min-w-0 flex-1 truncate text-[13px] text-foreground"
          :class="needsAttention || props.conversation.unread ? 'font-semibold' : 'font-medium'"
        >
          {{ title }}
        </span>
      </TooltipTrigger>
      <TooltipContent side="right" class="space-y-0.5 text-left">
        <div class="font-medium">{{ title }}</div>
        <div>{{ props.conversation.workspace_name }}</div>
        <div v-if="needsAttention" class="text-amber-200">{{ attentionLabel }}</div>
        <div class="text-background/70">{{ tooltipDate() }}</div>
      </TooltipContent>
    </Tooltip>

    <div class="relative flex h-6 w-20 shrink-0 items-center justify-end">
      <span
        class="max-w-full truncate text-right text-[11px] text-muted-foreground group-hover/row:invisible group-focus-within/row:invisible"
      >
        {{
          props.showWorkspace
            ? props.conversation.workspace_name
            : formatTimeAgo(props.conversation.updated_at)
        }}
      </span>
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <button
            type="button"
            data-testid="conversation-row-menu"
            class="absolute inset-y-0 right-0 hidden size-6 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:flex focus-visible:outline-2 focus-visible:outline-primary group-hover/row:flex group-focus-within/row:flex data-[state=open]:flex"
            :aria-label="`Aktionen für ${title}`"
            @click.stop
          >
            <MoreHorizontal class="size-3.5" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" class="w-48">
          <DropdownMenuItem @click="startRename">
            <Pencil class="size-4" />
            Umbenennen
          </DropdownMenuItem>
          <DropdownMenuItem
            v-if="props.conversation.unread"
            data-testid="mark-read-item"
            @click="emit('mark-read', props.conversation)"
          >
            <MailOpen class="size-4" />
            Als gelesen markieren
          </DropdownMenuItem>
          <DropdownMenuItem
            v-else
            data-testid="mark-unread-item"
            @click="emit('mark-unread', props.conversation)"
          >
            <Mail class="size-4" />
            Als ungelesen markieren
          </DropdownMenuItem>
          <DropdownMenuItem variant="destructive" @click="emit('delete', props.conversation)">
            <Trash2 class="size-4" />
            Löschen
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  </div>
</template>
