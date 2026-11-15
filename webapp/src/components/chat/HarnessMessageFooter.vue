<script setup lang="ts">
import { GitFork, Pencil } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import CopyButton from '@/components/common/CopyButton.vue'

withDefaults(
  defineProps<{
    align?: 'start' | 'end'
    copyText?: string | null
    copyLabel: 'message' | 'answer'
    editable?: boolean
    forkable?: boolean
    metadata?: string | null
    streaming?: boolean
  }>(),
  { align: 'start' },
)

const emit = defineEmits<{
  edit: []
  fork: []
}>()

const actionClass =
  'h-7 w-7 shrink-0 text-muted-foreground hover:text-foreground [&_svg:not([class*=size-])]:size-3 [@media(pointer:coarse)]:h-8 [@media(pointer:coarse)]:w-8'
</script>

<template>
  <TooltipProvider>
    <div
      data-testid="message-footer"
      role="group"
      :aria-label="copyLabel === 'message' ? 'Message actions' : 'Answer actions'"
      class="mt-0.5 flex min-h-7 min-w-0 items-center gap-0.5 text-muted-foreground opacity-100 motion-safe:transition-opacity md:opacity-0 md:group-hover/message:opacity-100 md:group-focus-within/message:opacity-100 md:focus-within:opacity-100 [@media(hover:none)]:opacity-100"
      :class="align === 'end' ? 'justify-end' : 'justify-start'"
    >
      <Tooltip v-if="copyText">
        <TooltipTrigger as-child>
          <CopyButton
            :text="copyText"
            :label="copyLabel"
            :data-testid="copyLabel === 'message' ? 'message-copy' : 'answer-copy'"
            :class="actionClass"
            title=""
          />
        </TooltipTrigger>
        <TooltipContent side="top">Copy {{ copyLabel }}</TooltipContent>
      </Tooltip>
      <Tooltip v-if="editable">
        <TooltipTrigger as-child>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            :class="actionClass"
            data-testid="message-edit"
            aria-label="Edit message and rerun"
            @click="emit('edit')"
          >
            <Pencil />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top">Edit &amp; rerun</TooltipContent>
      </Tooltip>
      <Tooltip v-if="forkable">
        <TooltipTrigger as-child>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            :class="actionClass"
            data-testid="message-fork"
            aria-label="Fork chat after this answer"
            @click="emit('fork')"
          >
            <GitFork />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top">Fork after this answer</TooltipContent>
      </Tooltip>
      <template v-if="metadata">
        <span
          v-if="copyText || editable || forkable"
          class="mx-0.5 h-2.5 w-px shrink-0 bg-border"
          aria-hidden="true"
        />
        <Popover>
          <Tooltip>
            <TooltipTrigger as-child>
              <PopoverTrigger as-child>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  class="h-7 min-w-0 max-w-full shrink px-1 text-[11px] font-normal text-muted-foreground hover:text-foreground [@media(pointer:coarse)]:h-8"
                  :data-testid="
                    streaming ? 'harness-message-running-model' : 'harness-message-usage'
                  "
                  :aria-label="metadata"
                >
                  <span class="truncate">{{ metadata }}</span>
                </Button>
              </PopoverTrigger>
            </TooltipTrigger>
            <TooltipContent side="top" class="max-w-xs break-words">{{ metadata }}</TooltipContent>
          </Tooltip>
          <PopoverContent
            side="top"
            align="start"
            class="w-auto max-w-xs break-words p-3 text-xs"
            @open-auto-focus.prevent
          >
            {{ metadata }}
          </PopoverContent>
        </Popover>
      </template>
    </div>
  </TooltipProvider>
</template>
