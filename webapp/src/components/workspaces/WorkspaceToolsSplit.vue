<script setup lang="ts">
/**
 * WorkspaceToolsSplit — chat/home column plus the Git/Desktop/Terminal/Files
 * side panel. Moves the existing chat into the side panel while editing,
 * retaining its mounted instance. Owns file-socket routing and tool-store
 * reset when the workspace changes or this host unmounts.
 */
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, toRef, watch } from 'vue'
import DesktopSurface from '@/components/workspaces/DesktopSurface.vue'
import WorkspaceDesktop from '@/components/workspaces/WorkspaceDesktop.vue'
import { useWorkspaceFileEvents } from '@/composables/useWorkspaceFileEvents'
import { useDesktopStore } from '@/stores/desktop'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useWorkspaceImageStore } from '@/stores/workspaceImages'
import { useGitStore } from '@/stores/git'
import { useSidePanelStore } from '@/stores/sidePanel'
import { useTerminalStore } from '@/stores/terminal'
import { useEditorStore } from '@/stores/editor'
import EditorTabs from '@/components/editor/EditorTabs.vue'

const WorkspaceEditor = defineAsyncComponent(
  () => import('@/components/editor/WorkspaceEditor.vue'),
)
const WorkspaceSidePanel = defineAsyncComponent(
  () => import('@/components/workspaces/WorkspaceSidePanel.vue'),
)

// Media URLs are keyed by path in the store. Keep them across Home → chat
// transitions for the same workspace, but never reuse them in another one.
let mediaWorkspaceId: string | null = null

const props = defineProps<{
  workspaceId: string
  canPrompt: boolean
}>()

const sidePanelStore = useSidePanelStore()
const desktopStore = useDesktopStore()
const fileExplorerStore = useFileExplorerStore()
const workspaceImageStore = useWorkspaceImageStore()
if (props.workspaceId && mediaWorkspaceId !== props.workspaceId) {
  workspaceImageStore.reset()
  mediaWorkspaceId = props.workspaceId
}
const gitStore = useGitStore()
const terminalStore = useTerminalStore()
const editorStore = useEditorStore()
const workspaceEditor = ref<{
  save: () => Promise<void>
  reload: () => void
  download: () => void
} | null>(null)
const sidebarChatOpen = ref(false)
const sidebarChatTarget = ref<HTMLElement | null>(null)
watch(
  () => props.workspaceId,
  (id) => editorStore.setWorkspace(id),
  { immediate: true },
)

const fileMutations = new Set([
  'stage',
  'unstage',
  'stageAll',
  'unstageAll',
  'discard',
  'discardAll',
  'commit',
  'pull',
  'sync',
  'checkoutBranch',
  'checkoutCommit',
  'checkoutRemoteBranch',
  'mergeIntoCurrent',
  'mergeCurrentInto',
  'mergeAbort',
  'applyStash',
  'popStash',
  'createBranchFromStash',
])
const stopGitActions = gitStore.$onAction(({ name, after }) => {
  if (!fileMutations.has(name)) return
  const id = props.workspaceId
  after((success) => {
    if (success && id === props.workspaceId) void editorStore.refreshWorkspaceFiles(id)
  })
})

function refreshActiveFile(): void {
  if (!props.canPrompt || !editorStore.isOpen || document.visibilityState !== 'visible') return
  const tab = editorStore.activeTab
  if (tab && !tab.dirty && !tab.loading && !tab.saving && !tab.busy)
    void editorStore.reloadTab(tab.id)
}
onMounted(() => window.addEventListener('focus', refreshActiveFile))

useWorkspaceFileEvents(toRef(props, 'workspaceId'))

const showingEditor = computed(() => props.canPrompt && editorStore.isOpen)
const showingSidebarChat = computed(() => showingEditor.value && sidebarChatOpen.value)
const hasWorkspace = computed(() => props.workspaceId.length > 0)
const showSidePanel = computed(
  () =>
    props.canPrompt && hasWorkspace.value && (sidePanelStore.hasOpened || showingSidebarChat.value),
)
const sidePanelVisible = computed(() => sidePanelStore.isOpen || showingSidebarChat.value)

function toggleSidebarChat(): void {
  if (showingEditor.value) sidebarChatOpen.value = !sidebarChatOpen.value
}

function toggleSidePanel(): void {
  if (showingSidebarChat.value) {
    sidebarChatOpen.value = false
    sidePanelStore.close()
  } else sidePanelStore.toggle()
}

watch(showingEditor, (open) => {
  if (!open) sidebarChatOpen.value = false
})

function resetToolStores(options: { resetDesktop?: boolean } = {}): void {
  // On workspace switches DesktopSurface owns releasing its old runner lease.
  // Resetting this shared store first would erase the old workspace identity,
  // preventing the surface from issuing that release.
  if (options.resetDesktop !== false) desktopStore.reset()
  terminalStore.reset()
  fileExplorerStore.reset()
  gitStore.reset()
}

watch(
  () => props.canPrompt,
  (ok) => {
    if (!ok) {
      desktopStore.close()
      terminalStore.close()
      fileExplorerStore.reset()
      gitStore.reset()
    }
  },
)

watch(
  () => props.workspaceId,
  (newId, oldId) => {
    if (!oldId || newId === oldId) return
    sidebarChatOpen.value = false
    resetToolStores({ resetDesktop: false })
    if (newId && newId !== mediaWorkspaceId) {
      workspaceImageStore.reset()
      mediaWorkspaceId = newId
    }
  },
)

onUnmounted(() => {
  stopGitActions()
  window.removeEventListener('focus', refreshActiveFile)
  resetToolStores()
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-1 flex-col">
    <div class="flex min-h-0 flex-1" :class="{ 'max-lg:flex-col': showingSidebarChat }">
      <div class="flex min-h-0 min-w-0 flex-1 flex-col">
        <EditorTabs
          v-if="showingEditor"
          :chat-open="showingSidebarChat"
          :side-panel-open="sidePanelVisible"
          @save="workspaceEditor?.save()"
          @reload="workspaceEditor?.reload()"
          @download="workspaceEditor?.download()"
          @toggle-chat="toggleSidebarChat"
          @toggle-side-panel="toggleSidePanel"
        />
        <div v-show="!showingEditor"><slot name="header" /></div>
        <div class="flex min-h-0 flex-1 flex-col overflow-x-hidden">
          <WorkspaceEditor v-if="showingEditor" ref="workspaceEditor" />
          <Teleport
            :to="sidebarChatTarget ?? 'body'"
            :disabled="!showingSidebarChat || !sidebarChatTarget"
          >
            <div
              v-show="!showingEditor || showingSidebarChat"
              class="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
              data-testid="workspace-chat-content"
            >
              <slot />
            </div>
          </Teleport>
        </div>
      </div>

      <WorkspaceSidePanel
        v-if="showSidePanel"
        v-show="sidePanelVisible"
        :key="workspaceId"
        :workspace-id="workspaceId"
        :chat-open="showingSidebarChat"
        @close-chat="sidebarChatOpen = false"
      >
        <template #chat-header><slot name="chat-header">Chat</slot></template>
        <template #chat>
          <div
            ref="sidebarChatTarget"
            class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
            data-testid="workspace-sidebar-chat-target"
          />
        </template>
      </WorkspaceSidePanel>
    </div>

    <DesktopSurface
      v-if="canPrompt && hasWorkspace"
      :key="workspaceId"
      :workspace-id="workspaceId"
    />
    <WorkspaceDesktop v-if="canPrompt && hasWorkspace" :workspace-id="workspaceId" />
  </div>
</template>
