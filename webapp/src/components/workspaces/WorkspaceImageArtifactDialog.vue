<script setup lang="ts">
/**
 * Dialog for capturing an image directly from a workspace card.
 * Used when the camera icon is clicked in WorkspaceActions.
 */
import { ref, computed } from 'vue'
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
import CaptureTargetFields from '@/components/images/CaptureTargetFields.vue'
import { NEW_IMAGE } from '@/lib/imageVersions'
import { useWorkspaceStore } from '@/stores/workspaces'
import type { Workspace } from '@/types'

const props = defineProps<{
  workspace: Workspace
  open: boolean
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
}>()

const workspaceStore = useWorkspaceStore()

const error = ref('')
const target = ref(NEW_IMAGE)
const name = ref('')
const message = ref('')
const submitting = ref(false)
const fields = ref<InstanceType<typeof CaptureTargetFields> | null>(null)

const isValid = computed(
  () =>
    !!fields.value?.isValid &&
    props.workspace.runtime_type === 'qemu' &&
    !props.workspace.intervention_required &&
    !workspaceStore.isWorkspaceTransitioning(props.workspace.id) &&
    (props.workspace.status === 'running' || props.workspace.status === 'stopped'),
)

async function handleSubmit(): Promise<void> {
  if (!isValid.value || !fields.value) return
  submitting.value = true
  const ok = await workspaceStore.createImageArtifact(props.workspace.id, fields.value.payload())
  submitting.value = false
  if (ok) handleClose()
  else
    error.value =
      'Capture refused. A stopped workspace requires controlled credential scrub proof; resume and stop it if externally stopped. Check notifications and runner operations for the exact diagnostic.'
}

function handleClose(): void {
  emit('update:open', false)
  setTimeout(() => {
    name.value = ''
    message.value = ''
    error.value = ''
  }, 200)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (!v ? handleClose() : undefined)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Capture Image</DialogTitle>
        <DialogDescription>
          The workspace briefly stops and continues on the captured version. Files, chats and
          settings are kept; running processes stop.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="error" role="alert" class="text-destructive">{{ error }}</p>
        <form id="capture-image-form" @submit.prevent="handleSubmit">
          <CaptureTargetFields
            ref="fields"
            v-model:target="target"
            v-model:name="name"
            v-model:message="message"
            :workspace="workspace"
            :disabled="submitting || workspaceStore.isWorkspaceTransitioning(workspace.id)"
          />
          <p class="text-xs text-muted-foreground mt-2">
            Workspace: <span class="font-mono">{{ workspace.name }}</span>
          </p>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" @click="handleClose">Cancel</Button>
        <Button type="submit" form="capture-image-form" :disabled="!isValid || submitting">
          {{ submitting ? 'Capturing' : 'Capture Image' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
