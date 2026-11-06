<script setup lang="ts">
/**
 * Toolbar action while Computer Use drives the desktop. The viewer itself
 * stays fully visible but read-only (DesktopSurface blocks its input);
 * taking control stops the agent.
 */
import { useDesktopStore } from '@/stores/desktop'
import { Button } from '@/components/ui/button'
import { MousePointerClick } from '@lucide/vue'

defineProps<{ busy: boolean }>()
const emit = defineEmits<{ takeControl: [] }>()
const desktopStore = useDesktopStore()
</script>

<template>
  <Button
    v-if="desktopStore.isConnected && desktopStore.computerUseActive"
    variant="outline"
    size="xs"
    :disabled="busy"
    title="Computer Use is controlling this desktop. Taking control stops it."
    data-testid="desktop-take-control"
    @click="emit('takeControl')"
  >
    <MousePointerClick />
    Take control
  </Button>
</template>
