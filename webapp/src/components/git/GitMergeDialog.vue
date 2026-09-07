<script setup lang="ts">
/**
 * GitMergeDialog — confirmation for merging branches from the Git graph.
 *
 * Supports both directions: merging the selected branch into the current
 * branch and merging the current branch into the selected one.
 */
import { computed } from 'vue'
import { useGitStore } from '@/stores/git'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { GitMerge } from '@lucide/vue'

const props = defineProps<{
  open: boolean
  direction: 'into-current' | 'current-into'
  /** The branch the action was triggered on. */
  branch: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
}>()

const store = useGitStore()

const source = computed(() =>
  props.direction === 'into-current'
    ? props.branch
    : (store.currentRepo?.currentBranch ?? ''),
)
const target = computed(() =>
  props.direction === 'into-current'
    ? (store.currentRepo?.currentBranch ?? '')
    : props.branch,
)

function handleConfirm(): void {
  if (props.direction === 'into-current') {
    store.mergeIntoCurrent(props.branch)
  } else {
    store.mergeCurrentInto(props.branch)
  }
  emit('update:open', false)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent data-testid="git-merge-dialog">
      <DialogHeader>
        <DialogTitle>Merge branch</DialogTitle>
        <DialogDescription>
          Merge
          <span class="font-medium text-foreground">{{ source }}</span>
          into
          <span class="font-medium text-foreground">{{ target }}</span
          >. This creates a merge commit on
          <span class="font-medium text-foreground">{{ target }}</span
          >.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">
          Cancel
        </Button>
        <Button data-testid="git-merge-confirm" @click="handleConfirm">
          <GitMerge :size="14" />
          Merge
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
