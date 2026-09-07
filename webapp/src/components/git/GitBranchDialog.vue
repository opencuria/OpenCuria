<script setup lang="ts">
/**
 * GitBranchDialog — create or rename a branch in the Git tab graph.
 *
 * Create mode shows the base commit and a "check out" checkbox; rename
 * mode prefills the current name. All operations run against the mock git
 * store.
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

watch(
  () => props.open,
  (open) => {
    if (!open) return
    name.value = props.mode === 'rename' ? (props.branchName ?? '') : ''
    checkout.value = true
  },
)

const title = computed(() =>
  props.mode === 'create' ? 'Create branch' : 'Rename branch',
)
const isValid = computed(() => name.value.trim().length > 0)

const baseLabel = computed(() => {
  if (!props.fromHash) return ''
  const repo = store.currentRepo
  const branch = repo?.branches.find((b) => b.tipHash === props.fromHash)
  return branch ? branch.name : props.fromHash.slice(0, 7)
})

function handleSubmit(): void {
  if (!isValid.value) return
  const ok =
    props.mode === 'create'
      ? store.createBranch(
          name.value,
          props.fromHash ?? store.currentRepo?.headHash ?? '',
          checkout.value,
        )
      : store.renameBranch(props.branchName ?? '', name.value)
  if (ok) emit('update:open', false)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
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
          @submit.prevent="handleSubmit"
        >
          <div>
            <Label for="git-branch-name" class="mb-1.5 block text-sm font-medium">
              Branch name
            </Label>
            <Input
              id="git-branch-name"
              v-model="name"
              placeholder="e.g. feature/my-work"
              data-testid="git-branch-name"
            />
          </div>
          <div v-if="mode === 'create'" class="flex items-center gap-2">
            <Checkbox
              id="git-branch-checkout"
              v-model:checked="checkout"
              data-testid="git-branch-checkout"
            />
            <Label for="git-branch-checkout" class="text-sm font-normal">
              Check out new branch
            </Label>
          </div>
        </form>
      </DialogBody>
      <DialogFooter>
        <Button variant="outline" @click="emit('update:open', false)">
          Cancel
        </Button>
        <Button
          type="submit"
          form="git-branch-form"
          :disabled="!isValid"
          data-testid="git-branch-submit"
        >
          {{ mode === 'create' ? 'Create' : 'Rename' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
