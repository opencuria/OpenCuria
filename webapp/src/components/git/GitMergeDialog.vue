<script setup lang="ts">
/**
 * GitMergeDialog — confirmation for merging branches from the Git graph.
 *
 * Supports both directions: merging the selected branch into the current
 * branch and merging the current branch into the selected one. Like
 * standard Git / VS Code, a fast-forward is used when possible, otherwise
 * a merge commit is created.
 *
 * On an HTTP 409 merge conflict the fresh snapshot is applied, the store
 * flags `lastConflict`, and the dialog closes so the user is guided to the
 * merge banner in the Changes section. Generic failures keep the dialog
 * open.
 */
import { computed, ref, watch } from 'vue'
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
const submitting = ref(false)

watch(
  () => props.open,
  (open) => {
    if (open) submitting.value = false
  },
)

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

const isBusy = computed(() => store.busyOperation !== null)
const canConfirm = computed(
  () =>
    !submitting.value &&
    !isBusy.value &&
    source.value.length > 0 &&
    target.value.length > 0 &&
    source.value !== target.value,
)

function requestClose(): void {
  if (submitting.value) return
  emit('update:open', false)
}

async function handleConfirm(): Promise<void> {
  if (!canConfirm.value) return
  submitting.value = true
  try {
    const ok =
      props.direction === 'into-current'
        ? await store.mergeIntoCurrent(props.branch)
        : await store.mergeCurrentInto(props.branch)
    // Conflict: the snapshot (merge banner) is already applied — close so
    // the user continues in the Changes section.
    if (ok || store.lastConflict) emit('update:open', false)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => !v && requestClose()">
    <DialogContent data-testid="git-merge-dialog">
      <DialogHeader>
        <DialogTitle>Merge branch</DialogTitle>
        <DialogDescription>
          Merge
          <span class="font-medium text-foreground">{{ source }}</span>
          into
          <span class="font-medium text-foreground">{{ target }}</span
          >. Uses fast-forward when possible, otherwise creates a merge
          commit on
          <span class="font-medium text-foreground">{{ target }}</span
          >.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button
          variant="outline"
          :disabled="submitting"
          data-testid="git-merge-cancel"
          @click="requestClose"
        >
          Cancel
        </Button>
        <Button
          :disabled="!canConfirm"
          data-testid="git-merge-confirm"
          @click="void handleConfirm()"
        >
          <GitMerge :size="14" />
          {{ submitting ? 'Merging…' : 'Merge' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
