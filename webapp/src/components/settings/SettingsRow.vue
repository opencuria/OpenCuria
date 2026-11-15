<script setup lang="ts">
import type { HTMLAttributes } from 'vue'
import { cn } from '@/lib/utils'

const props = defineProps<{
  class?: HTMLAttributes['class']
  iconClass?: HTMLAttributes['class']
  bareIcon?: boolean
}>()
</script>

<template>
  <div :class="cn('px-4 py-4', props.class)">
    <div class="flex items-start gap-3">
      <div
        v-if="$slots.icon"
        :class="
          bareIcon
            ? 'shrink-0'
            : cn(
                'flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground',
                props.iconClass,
              )
        "
      >
        <slot name="icon" />
      </div>
      <div class="min-w-0 flex-1">
        <slot />
      </div>
      <div v-if="$slots.badges" class="flex shrink-0 flex-wrap items-center gap-1.5">
        <slot name="badges" />
      </div>
      <div v-if="$slots.actions" class="flex shrink-0 items-center gap-1">
        <slot name="actions" />
      </div>
    </div>
    <div v-if="$slots.detail" class="mt-3" :class="$slots.icon && !bareIcon ? 'pl-12' : undefined">
      <slot name="detail" />
    </div>
  </div>
</template>
