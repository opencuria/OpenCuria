<script setup lang="ts">
import { Button } from '@/components/ui/button'
import type { AutosaveState } from '@/composables/useAutosave'

defineProps<{ state: AutosaveState }>()
</script>

<template>
  <div class="flex items-center gap-3 text-xs" role="status" aria-live="polite">
    <span :class="state.error ? 'text-destructive' : 'text-muted-foreground'">
      {{
        state.saving
          ? 'Saving…'
          : state.error
            ? `Changes not saved: ${state.error}`
            : state.dirty
              ? state.valid
                ? 'Waiting to save…'
                : 'Check your input'
              : 'All changes saved'
      }}
    </span>
    <Button
      v-if="state.error"
      type="button"
      variant="outline"
      size="sm"
      :disabled="state.saving"
      @click="state.flush()"
    >
      Retry
    </Button>
  </div>
</template>
