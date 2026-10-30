<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref } from 'vue'
import { AlertTriangle } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { useEditorStore } from '@/stores/editor'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useGitStore } from '@/stores/git'
import type { MonacoChangeRange } from '@/lib/monacoChanges'

const MonacoEditor = defineAsyncComponent(() => import('./MonacoEditor.vue'))
const store = useEditorStore()
const files = useFileExplorerStore()
const git = useGitStore()
const tab = computed(() => store.activeTab)
const pendingReload = ref<string | null>(null)
const pendingTab = computed(() =>
  store.tabs.find((item) => item.id === (pendingReload.value ?? store.pendingCloseId)),
)
const dialogOpen = computed(() => Boolean(pendingTab.value))

function cancelDialog(): void {
  pendingReload.value = null
  store.pendingCloseId = null
}

async function discardPending(): Promise<void> {
  const id = pendingTab.value?.id
  if (!id) return
  if (pendingReload.value) await store.reloadTab(id, true)
  else store.closeTab(id, true)
  cancelDialog()
}

async function savePending(): Promise<void> {
  const id = pendingTab.value?.id
  if (!id || !(await store.saveTab(id))) return
  store.closeTab(id)
  cancelDialog()
  void git.refresh({ silent: true, forceDetails: true })
}

async function save(): Promise<void> {
  if (tab.value && (await store.saveTab(tab.value.id)))
    void git.refresh({ silent: true, forceDetails: true })
}

function reload(): void {
  if (!tab.value) return
  if (tab.value.dirty) pendingReload.value = tab.value.id
  else void store.reloadTab(tab.value.id)
}

function download(): void {
  if (!tab.value) return
  void files.downloadFile(
    tab.value.workspaceId,
    tab.value.repoPath ? `${tab.value.repoPath}/${tab.value.path}` : tab.value.path,
  )
}

defineExpose({ save, reload, download })

async function applyChange(action: 'stage' | 'revert', range: MonacoChangeRange): Promise<void> {
  if (!tab.value) return
  if (await store.applyChange(tab.value.id, action, range))
    void git.refresh({ silent: true, forceDetails: true })
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.defaultPrevented || !(event.ctrlKey || event.metaKey) || !store.isOpen) return
  if (event.key.toLowerCase() === 's' && !event.shiftKey && !event.altKey) {
    event.preventDefault()
    void save()
  } else if (event.key.toLowerCase() === 'w' && !event.shiftKey && !event.altKey && tab.value) {
    event.preventDefault()
    store.closeTab(tab.value.id)
  }
}

onMounted(() => document.addEventListener('keydown', handleKeydown))
onUnmounted(() => document.removeEventListener('keydown', handleKeydown))
</script>

<template>
  <section
    v-if="tab"
    :id="`editor-panel-${tab.id}`"
    role="tabpanel"
    class="flex min-h-0 min-w-0 flex-1 flex-col bg-background"
    data-testid="workspace-editor"
  >
    <div v-if="tab.loading && !tab.loaded" class="space-y-2 p-4" data-testid="editor-loading">
      <Skeleton
        v-for="i in 12"
        :key="i"
        class="h-3.5"
        :style="{ width: `${88 - (i % 4) * 14}%` }"
      />
    </div>
    <div
      v-else-if="tab.error && !tab.dirty && !tab.loaded"
      class="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center"
      data-testid="editor-error"
    >
      <AlertTriangle :size="26" class="text-warning" />
      <p class="max-w-lg text-sm text-muted-foreground">{{ tab.error }}</p>
      <Button variant="outline" size="sm" @click="reload">Retry</Button>
    </div>
    <template v-else>
      <div
        v-if="tab.error"
        role="alert"
        class="flex shrink-0 items-center gap-2 border-b border-warning/20 bg-warning/10 px-3 py-2 text-xs text-warning"
        data-testid="editor-save-error"
      >
        <AlertTriangle :size="14" class="shrink-0" />
        <span class="flex-1">{{ tab.error }}</span>
        <Button variant="ghost" size="sm" @click="reload">Reload</Button>
      </div>
      <div
        v-if="tab.mediaType === 'image'"
        class="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-muted/20 p-4"
        data-testid="editor-image"
      >
        <img
          :src="`data:${tab.mimeType};base64,${tab.rawBase64}`"
          :alt="tab.path"
          class="max-h-full max-w-full object-contain"
        />
      </div>
      <iframe
        v-else-if="tab.mediaType === 'pdf'"
        :src="`data:application/pdf;base64,${tab.rawBase64}`"
        title="PDF preview"
        class="min-h-0 w-full flex-1 border-0"
      />
      <div
        v-else-if="tab.mediaType === 'binary'"
        class="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground"
        data-testid="editor-binary"
      >
        Binary file — download to view
      </div>
      <div
        v-else-if="tab.truncated"
        class="shrink-0 border-b border-warning/20 bg-warning/10 px-3 py-2 text-xs text-warning"
        data-testid="editor-truncated"
      >
        Read-only preview · {{ files.formatBytes(tab.previewBytes) }} of
        {{ files.formatBytes(tab.size) }}
      </div>
      <MonacoEditor
        v-if="tab.mediaType === 'text'"
        :key="tab.id"
        :model-key="tab.id"
        :path="tab.path"
        :model-value="tab.content"
        :original="tab.originalContent"
        :diff="tab.kind !== 'file'"
        :staged="tab.staged"
        :read-only="tab.readOnly || tab.loading"
        :git-actions-enabled="tab.kind === 'diff' && !tab.dirty"
        :busy="tab.busy || tab.saving || tab.loading"
        class="min-h-0 flex-1"
        @update:model-value="store.updateContent(tab!.id, $event)"
        @save="save"
        @stage="applyChange('stage', $event)"
        @discard="applyChange('revert', $event)"
      />
      <div
        class="flex h-6 shrink-0 items-center gap-3 border-t border-border px-3 text-[10px] text-muted-foreground"
      >
        <span v-if="tab.saving">Saving…</span>
        <span v-else-if="tab.dirty">Unsaved changes</span>
        <span v-else-if="tab.readOnly">Read-only</span>
        <span v-else>Saved</span>
        <span v-if="tab.kind === 'diff' && tab.dirty">Save to stage or revert changes</span>
        <span class="ml-auto">{{ tab.mediaType === 'text' ? 'UTF-8' : tab.mimeType }}</span>
        <span v-if="tab.mediaType === 'text'">{{
          tab.content.includes('\r\n') ? 'CRLF' : 'LF'
        }}</span>
      </div>
    </template>
  </section>

  <Dialog :open="dialogOpen" @update:open="!$event && cancelDialog()">
    <DialogContent :show-close-button="false" data-testid="editor-unsaved-dialog">
      <DialogHeader>
        <DialogTitle>{{
          pendingReload ? 'Discard changes and reload?' : `Save changes to ${pendingTab?.name}?`
        }}</DialogTitle>
        <DialogDescription>Your changes will be lost if you don't save them.</DialogDescription>
      </DialogHeader>
      <DialogFooter class="flex-wrap">
        <Button variant="outline" @click="cancelDialog">Cancel</Button>
        <Button variant="outline" :disabled="pendingTab?.saving" @click="discardPending">{{
          pendingReload ? 'Discard and reload' : "Don't save"
        }}</Button>
        <Button v-if="!pendingReload" :disabled="pendingTab?.saving" @click="savePending">{{
          pendingTab?.saving ? 'Saving…' : 'Save'
        }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
