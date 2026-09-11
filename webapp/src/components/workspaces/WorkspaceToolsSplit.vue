<script setup lang="ts">
/**
 * WorkspaceToolsSplit — chat/home column plus the Git/Desktop/Terminal/Files
 * side panel. Overlays FileViewer / GitDiffViewer on the default slot while
 * keeping slot content mounted. Owns file-socket routing and tool-store
 * reset when the workspace changes or this host unmounts.
 */
import { computed, onUnmounted, toRef, watch } from 'vue'
import FileViewer from '@/components/files/FileViewer.vue'
import GitDiffViewer from '@/components/git/GitDiffViewer.vue'
import DesktopSurface from '@/components/workspaces/DesktopSurface.vue'
import WorkspaceDesktop from '@/components/workspaces/WorkspaceDesktop.vue'
import WorkspaceSidePanel from '@/components/workspaces/WorkspaceSidePanel.vue'
import { useWorkspaceFileEvents } from '@/composables/useWorkspaceFileEvents'
import { useDesktopStore } from '@/stores/desktop'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useGitStore } from '@/stores/git'
import { useSidePanelStore } from '@/stores/sidePanel'
import { useTerminalStore } from '@/stores/terminal'

const props = defineProps<{
  workspaceId: string
  canPrompt: boolean
}>()

const sidePanelStore = useSidePanelStore()
const desktopStore = useDesktopStore()
const fileExplorerStore = useFileExplorerStore()
const gitStore = useGitStore()
const terminalStore = useTerminalStore()

useWorkspaceFileEvents(toRef(props, 'workspaceId'))

const showingGitDiff = computed(
  () => Boolean(gitStore.viewingDiffChange || gitStore.viewingCommitDiff),
)
const showingFile = computed(
  () => fileExplorerStore.isViewingFile || fileExplorerStore.isLoadingContent,
)
const showingOverlay = computed(() => showingGitDiff.value || showingFile.value)
const hasWorkspace = computed(() => props.workspaceId.length > 0)
const showSidePanel = computed(
  () => props.canPrompt && hasWorkspace.value && sidePanelStore.hasOpened,
)

function resetToolStores(): void {
  desktopStore.reset()
  terminalStore.reset()
  fileExplorerStore.reset()
  gitStore.reset()
}

watch(
  () => props.canPrompt,
  (ok) => {
    if (!ok) desktopStore.close()
  },
)

watch(
  () => props.workspaceId,
  (newId, oldId) => {
    if (!oldId || newId === oldId) return
    resetToolStores()
  },
)

onUnmounted(() => {
  resetToolStores()
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-1 flex-col">
    <div class="flex min-h-0 flex-1">
      <div class="flex min-w-0 flex-1 flex-col">
        <slot name="header" />
        <div class="flex min-h-0 flex-1 flex-col overflow-x-hidden">
          <GitDiffViewer
            v-if="showingGitDiff"
            :workspace-id="workspaceId"
          />
          <FileViewer
            v-else-if="showingFile"
            :workspace-id="workspaceId"
          />
          <div
            v-show="!showingOverlay"
            class="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
          >
            <slot />
          </div>
        </div>
      </div>

      <WorkspaceSidePanel
        v-if="showSidePanel"
        v-show="sidePanelStore.isOpen"
        :key="workspaceId"
        :workspace-id="workspaceId"
      />
    </div>

    <DesktopSurface
      v-if="canPrompt && hasWorkspace"
      :workspace-id="workspaceId"
    />
    <WorkspaceDesktop
      v-if="hasWorkspace"
      :workspace-id="workspaceId"
    />
  </div>
</template>
