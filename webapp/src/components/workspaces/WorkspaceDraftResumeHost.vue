<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useNotificationStore } from '@/stores/notifications'
import { readWorkspaceDraft, removeWorkspaceDraft } from '@/lib/workspaceDraft'
import CreateWorkspaceDialog from './CreateWorkspaceDialog.vue'
import EditWorkspaceDialog from './EditWorkspaceDialog.vue'
import type { WorkspaceDraft } from '@/lib/workspaceDraft'
import type { Workspace } from '@/types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const workspaces = useWorkspaceStore()
const notifications = useNotificationStore()
const draft = ref<WorkspaceDraft | null>(null)
const createOpen = ref(false)
const createDraftId = ref<string>()
const editWorkspace = ref<Workspace | null>(null)
const editDraftId = ref<string>()
const editOpen = ref(false)
const invalidMessage = ref<string | null>(null)
const resumeId = computed(() =>
  typeof route.query.resume_workspace === 'string' ? route.query.resume_workspace : null,
)
let resumeGeneration = 0
let disposed = false

watch(
  [resumeId, () => auth.initialized, () => auth.user?.id, () => auth.activeOrganizationId],
  async ([id, initialized, userId, organizationId]) => {
    if (!id || !initialized || userId === undefined || !organizationId) return
    const requestId = ++resumeGeneration
    const query = { ...route.query }
    delete query.resume_workspace
    void router.replace({ path: route.path, query }).catch(() => undefined)
    if (createDraftId.value && createDraftId.value !== id) removeWorkspaceDraft(createDraftId.value)
    if (editDraftId.value && editDraftId.value !== id) removeWorkspaceDraft(editDraftId.value)
    clearEdit()
    createDraftId.value = id
    const result = readWorkspaceDraft(id, {
      userId,
      organizationId,
    })
    if (result.status !== 'ok') {
      invalidMessage.value = `Workspace draft is unavailable (${result.status}). No workspace was changed.`
      notifications.error('Draft unavailable', invalidMessage.value)
      return
    }
    draft.value = result.draft
    invalidMessage.value = null
    if (result.draft.fields.mode === 'create') return
    await workspaces.fetchWorkspaces()
    if (
      disposed ||
      requestId !== resumeGeneration ||
      auth.activeOrganizationId !== organizationId ||
      auth.user?.id !== userId
    )
      return
    const workspaceId = result.draft.fields.workspaceId
    const target = workspaces.workspaces.find((entry) => entry.id === workspaceId)
    if (!target) {
      removeWorkspaceDraft(id)
      invalidMessage.value =
        'The workspace for this edit draft is no longer available in the current organization.'
      notifications.error('Draft unavailable', invalidMessage.value)
      return
    }
    editWorkspace.value = target
    editDraftId.value = id
    editOpen.value = true
  },
  { immediate: true },
)
watch(
  [() => auth.user?.id, () => auth.activeOrganizationId],
  ([userId, organizationId], [previousUserId, previousOrganizationId]) => {
    if (
      previousUserId === undefined ||
      previousOrganizationId === undefined ||
      (userId === previousUserId && organizationId === previousOrganizationId)
    )
      return
    resumeGeneration += 1
    clearCreate()
    clearEdit()
    invalidMessage.value =
      'Account or organization changed. The saved workspace draft is no longer available.'
  },
)
onUnmounted(() => {
  disposed = true
  resumeGeneration += 1
})

watch(editOpen, (value) => {
  if (!value) clearEdit()
})
function clearCreate(_workspaceId?: string | null): void {
  createOpen.value = false
  createDraftId.value = undefined
}
function clearEdit(): void {
  editOpen.value = false
  editDraftId.value = undefined
  editWorkspace.value = null
}
</script>

<template>
  <div
    v-if="invalidMessage"
    class="fixed bottom-4 right-4 z-50 max-w-md rounded-md border border-destructive/30 bg-background px-4 py-3 text-sm text-destructive shadow-lg"
    role="alert"
    data-testid="workspace-draft-resume-error"
  >
    {{ invalidMessage }}
    <button type="button" class="ml-2 underline" @click="invalidMessage = null">Dismiss</button>
  </div>
  <CreateWorkspaceDialog
    v-if="createDraftId"
    :key="createDraftId"
    :resume-draft-id="createDraftId"
    hide-trigger
    @handoff="clearCreate"
    @created="clearCreate"
  />
  <EditWorkspaceDialog
    v-if="editWorkspace && editDraftId"
    v-model:open="editOpen"
    :workspace="editWorkspace"
    :resume-draft-id="editDraftId"
    hide-trigger
    @draft-error="(message) => notifications.error('Draft not restored', message)"
  />
</template>
