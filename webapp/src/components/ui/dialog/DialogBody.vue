<script setup lang="ts">
import type { HTMLAttributes } from "vue"

import { cn } from "@/lib/utils"

const props = defineProps<{ class?: HTMLAttributes["class"] }>()
</script>

<template>
  <div
    data-slot="dialog-body"
    :class="cn('flex-1 min-h-0 overflow-y-auto -mx-6 px-6', props.class)"
  >
    <slot />
  </div>
</template>
