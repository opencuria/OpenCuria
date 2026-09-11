<script setup lang="ts">
/**
 * SidePanelToggle — icon button that opens or closes the workspace side panel.
 */
import { Button } from '@/components/ui/button'
import { PanelRight } from '@lucide/vue'

withDefaults(
  defineProps<{
    open: boolean
    disabled?: boolean
  }>(),
  { disabled: false },
)

const emit = defineEmits<{
  toggle: []
}>()
</script>

<template>
  <Button
    variant="ghost"
    size="icon-sm"
    :title="open ? 'Close side panel' : 'Open side panel'"
    :disabled="disabled"
    data-testid="workspace-chat-header-toggle-side-panel"
    @click="emit('toggle')"
  >
    <PanelRight :size="16" :class="open ? 'text-primary' : ''" />
  </Button>
</template>
