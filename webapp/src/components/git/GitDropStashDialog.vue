<script setup lang="ts">
/**
 * GitDropStashDialog — confirm dropping a stash entry.
 *
 * Dropping is destructive and irreversible (the entry and its WIP
 * commit leave the stash list). The dialog only closes after success;
 * a local `submitting` flag prevents double submits.
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
  /** Stash selector, e.g. `stash@{0}`. */
  selector: string
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

const isBusy = computed(() => store.busyOperation !== null)
const canConfirm = computed(
  () => props.selector.trim().length > 0 && !isBusy.value && !submitting.value,
)

function requestClose(): void {
  if (submitting.value) return
  emit('update:open', false)
}

async function handleConfirm(): Promise<void> {
  if (!canConfirm.value) return
  submitting.value = true
  try {
    const ok = await store.dropStash(props.selector)
    if (ok) emit('update:open', false)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => !v && requestClose()">
    <DialogContent data-testid="git-drop-stash-dialog">
      <DialogHeader>
        <DialogTitle>Drop stash</DialogTitle>
        <DialogDescription>
          Drop stash entry
          <span class="font-medium text-foreground">{{ selector }}</span
          >? This permanently removes the stashed changes.
        </DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button
          variant="outline"
          :disabled="submitting"
          data-testid="git-drop-stash-cancel"
          @click="requestClose"
        >
          Cancel
        </Button>
        <Button
          variant="destructive"
          :disabled="!canConfirm"
          data-testid="git-drop-stash-confirm"
          @click="void handleConfirm()"
        >
          {{ submitting ? 'Dropping…' : 'Drop stash' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
