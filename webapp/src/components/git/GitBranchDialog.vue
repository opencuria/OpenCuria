<script setup lang="ts">
/**
 * GitBranchDialog — create or rename a branch in the Git tab graph.
 *
 * Create mode shows the base commit and a "check out" checkbox; rename
 * mode prefills the current name. Operations run against the productive
 * git store (async); the dialog only closes after success and a local
 * `submitting` flag prevents double submits.
 */
import { computed, ref, watch } from 'vue'
import { useGitStore } from '@/stores/git'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'

const props = defineProps<{
  open: boolean
  mode: 'create' | 'rename'
  /** Existing branch name (rename mode). */
  branchName?: string
  /** Base commit hash (create mode). */
  fromHash?: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
}>()

const store = useGitStore()

const name = ref('')
const checkout = ref(true)
const submitting = ref(false)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    name.value = props.mode === 'rename' ? (props.branchName ?? '') : ''
    checkout.value = true
    submitting.value = false
  },
)

const title = computed(() =>
  props.mode === 'create' ? 'Create branch' : 'Rename branch',
)
const isBusy = computed(() => store.busyOperation !== null)
const isValid = computed(() => name.value.trim().length > 0)
const canSubmit = computed(
  () => isValid.value && !isBusy.value && !submitting.value,
)

const baseLabel = computed(() => {
  if (!props.fromHash) return ''
  const repo = store.currentRepo
  const branch = repo?.branches.find((b) => b.tipHash === props.fromHash)
  return branch ? branch.name : props.fromHash.slice(0, 7)
})

function requestClose(): void {
  if (submitting.value) return
  emit('update:open', false)
}

async function handleSubmit(): Promise<void> {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    const ok =
      props.mode === 'create'
        ? await store.createBranch(
            name.value,
            props.fromHash ?? store.currentRepo?.headHash ?? '',
            checkout.value,
          )
        : await store.renameBranch(props.branchName ?? '', name.value)
    if (ok) emit('update:open', false)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => !v && requestClose()">
    <DialogContent data-testid="git-branch-dialog">
      <DialogHeader>
        <DialogTitle>{{ title }}</DialogTitle>
        <DialogDescription v-if="mode === 'create'">
          Create a new branch at
          <span class="font-medium text-foreground">{{ baseLabel }}</span
          >.
        </DialogDescription>
        <DialogDescription v-else>
          Rename
          <span class="font-medium text-foreground">{{ branchName }}</span
          >.
        </DialogDescription>
      </DialogHeader>
      <DialogBody>
        <form
          id="git-branch-form"
          class="flex flex-col gap-4"
          @submit.prevent="void handleSubmit()"
        >
          <div>
            <Label for="git-branch-name" class="mb-1.5 block text-sm font-medium">
              Branch name
            </Label>
            <Input
              id="git-branch-name"
              v-model="name"
              placeholder="e.g. feature/my-work"
              :disabled="submitting || isBusy"
              data-testid="git-branch-name"
            />
          </div>
          <div v-if="mode === 'create'" class="flex items-center gap-2">
            <Checkbox
              id="git-branch-checkout"
              v-model:checked="checkout"
              :disabled="submitting || isBusy"
              data-testid="git-branch-checkout"
            />
            <Label for="git-branch-checkout" class="text-sm font-normal">
              Check out new branch
            </Label>
          </div>
        </form>
      </DialogBody>
      <DialogFooter>
        <Button
          variant="outline"
          :disabled="submitting"
          data-testid="git-branch-cancel"
          @click="requestClose"
        >
          Cancel
        </Button>
        <Button
          type="submit"
          form="git-branch-form"
          :disabled="!canSubmit"
          data-testid="git-branch-submit"
        >
          {{ submitting ? (mode === 'create' ? 'Creating…' : 'Renaming…') : mode === 'create' ? 'Create' : 'Rename' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
