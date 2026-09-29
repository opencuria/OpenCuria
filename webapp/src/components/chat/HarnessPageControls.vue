<script setup lang="ts">
import { computed } from 'vue'
import { Button } from '@/components/ui/button'
import { HARNESS_PAGE_SIZE } from '@/lib/harnessPagination'

const props = withDefaults(
  defineProps<{
    page: number
    totalItems: number
    label: string
  }>(),
  { totalItems: 0 },
)

const emit = defineEmits<{
  previous: []
  next: []
}>()

const pageCount = computed(() => Math.ceil(props.totalItems / HARNESS_PAGE_SIZE))
const firstItem = computed(() => props.page * HARNESS_PAGE_SIZE + 1)
const lastItem = computed(() => Math.min((props.page + 1) * HARNESS_PAGE_SIZE, props.totalItems))
</script>

<template>
  <nav
    v-if="totalItems > HARNESS_PAGE_SIZE"
    :aria-label="label"
    class="flex items-center justify-between gap-2 py-1 text-xs text-muted-foreground"
    data-testid="harness-page-controls"
  >
    <Button
      type="button"
      variant="ghost"
      size="sm"
      class="h-7 px-2"
      aria-label="Previous page"
      data-testid="harness-page-previous"
      :disabled="page === 0"
      @click="emit('previous')"
    >
      Previous
    </Button>
    <span class="text-center" data-testid="harness-page-status" aria-live="polite">
      Page {{ page + 1 }} of {{ pageCount }} · items {{ firstItem }}–{{ lastItem }} of
      {{ totalItems }}
    </span>
    <Button
      type="button"
      variant="ghost"
      size="sm"
      class="h-7 px-2"
      aria-label="Next page"
      data-testid="harness-page-next"
      :disabled="page + 1 >= pageCount"
      @click="emit('next')"
    >
      Next
    </Button>
  </nav>
</template>
