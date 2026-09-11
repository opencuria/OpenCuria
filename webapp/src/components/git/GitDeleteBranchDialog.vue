<script setup lang="ts">
/**
 * GitDeleteBranchDialog — confirm deleting a local branch.
 *
 * Uses `git branch -d` on the backend (safe delete: only fully merged
 * branches can be removed). The dialog only closes after success; a local
 * `submitting` flag prevents double submits.
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

const props = defineProps<{
  open: boolean
  /** Local branch to delete. */
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

const isCurrent = computed(
  () => store.currentRepo?.currentBranch === props.branch,
)
const isBusy = computed(() => store.busyOperation !== null)
const canConfirm = computed(
  () => props.branch.trim().length > 0 && !isCurrent.value && !isBusy.value && !submitting.value,
)

function requestClose(): void {
  if (submitting.value) return
  emit('update:open', false)
}

async function handleConfirm(): Promise<void> {
  if (!canConfirm.value) return
  submitting.value = true
  try {
    const ok = await store.deleteBranch(props.branch)
    if (ok) emit('update:open', false)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => !v && requestClose()">
    <DialogContent data-testid="git-delete-branch-dialog">
      <DialogHeader>
        <DialogTitle>Delete branch</DialogTitle>
        <DialogDescription>
          Delete local branch
          <span class="font-medium text-foreground">{{ branch }}</span
          >? Only fully merged branches can be safely deleted
          (<span class="font-mono">git branch -d</span> on the backend).
          Unmerged work cannot be deleted from here.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button
          variant="outline"
          :disabled="submitting"
          data-testid="git-delete-branch-cancel"
          @click="requestClose"
        >
          Cancel
        </Button>
        <Button
          variant="destructive"
          :disabled="!canConfirm"
          data-testid="git-delete-branch-confirm"
          @click="void handleConfirm()"
        >
          {{ submitting ? 'Deleting…' : 'Delete branch' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
