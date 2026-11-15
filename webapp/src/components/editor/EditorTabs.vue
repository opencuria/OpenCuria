<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Download, Loader2, MessageSquare, RefreshCw, Save, X } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { SidebarTrigger } from '@/components/ui/sidebar'
import WorkspaceFileIcon from '@/components/files/WorkspaceFileIcon.vue'
import SidePanelToggle from '@/components/chat/SidePanelToggle.vue'
import { useEditorStore } from '@/stores/editor'

withDefaults(defineProps<{ chatOpen?: boolean; sidePanelOpen?: boolean }>(), {
  chatOpen: false,
  sidePanelOpen: false,
})
const emit = defineEmits<{
  save: []
  reload: []
  download: []
  'toggle-chat': []
  'toggle-side-panel': []
}>()
const store = useEditorStore()
const tabList = ref<HTMLElement>()
const activeTab = computed(() => store.activeTab)

watch(
  () => store.activeTab?.id,
  async () => {
    await nextTick()
    tabList.value
      ?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  },
)

async function moveTab(event: KeyboardEvent, index: number): Promise<void> {
  let next = index
  if (event.key === 'ArrowRight') next = (index + 1) % store.tabs.length
  else if (event.key === 'ArrowLeft') next = (index - 1 + store.tabs.length) % store.tabs.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = store.tabs.length - 1
  else return
  event.preventDefault()
  const tab = store.tabs[next]
  if (!tab) return
  store.activateTab(tab.id)
  await nextTick()
  tabList.value?.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus()
}
</script>

<template>
  <header
    class="flex h-10 min-w-0 shrink-0 items-center border-b border-border bg-card"
    data-testid="editor-tabs"
  >
    <SidebarTrigger class="mx-1 shrink-0 text-muted-foreground" />
    <div
      ref="tabList"
      role="tablist"
      aria-label="Open files"
      class="flex h-full min-w-0 flex-1 overflow-x-auto"
    >
      <div
        v-for="(tab, index) in store.tabs"
        :key="tab.id"
        class="group relative flex h-full shrink-0 items-center border-r border-border"
        :class="
          store.activeTab?.id === tab.id
            ? 'bg-background text-foreground'
            : 'text-muted-foreground hover:bg-muted/50'
        "
        @mousedown.middle.prevent="store.closeTab(tab.id)"
      >
        <span
          v-if="store.activeTab?.id === tab.id"
          class="absolute inset-x-0 top-0 h-px bg-primary"
        />
        <Button
          role="tab"
          variant="ghost"
          class="h-full max-w-64 gap-2 rounded-none px-3 pr-1 text-xs font-normal hover:bg-transparent"
          :aria-selected="store.activeTab?.id === tab.id"
          :aria-controls="`editor-panel-${tab.id}`"
          :tabindex="store.activeTab?.id === tab.id ? 0 : -1"
          :title="`${tab.path}${tab.dirty ? ' • Unsaved changes' : ''}`"
          @click="store.activateTab(tab.id)"
          @keydown="moveTab($event, index)"
          @keydown.delete.prevent="store.closeTab(tab.id)"
        >
          <WorkspaceFileIcon :path="tab.path" :size="15" />
          <span class="truncate">{{ tab.name }}</span>
          <span v-if="tab.kind === 'diff'" class="text-[10px] text-muted-foreground">{{
            tab.staged ? 'Index' : 'Changes'
          }}</span>
          <span v-else-if="tab.kind === 'commit'" class="text-[10px] text-muted-foreground">{{
            tab.commitHash?.slice(0, 7)
          }}</span>
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          class="mr-1 size-6 shrink-0 rounded-sm"
          :aria-label="`Close ${tab.name}${tab.dirty ? ' (unsaved changes)' : ''}`"
          :title="tab.dirty ? 'Unsaved changes • Close' : 'Close'"
          :data-dirty="tab.dirty || undefined"
          :disabled="tab.saving || tab.busy"
          data-testid="editor-tab-close"
          @click="store.closeTab(tab.id)"
        >
          <span
            v-if="tab.dirty"
            class="size-2 rounded-full bg-current group-hover:hidden"
            data-testid="editor-tab-dirty"
          />
          <X :size="13" :class="tab.dirty ? 'hidden group-hover:block' : ''" />
        </Button>
      </div>
    </div>
    <div class="flex shrink-0 items-center gap-0.5 px-1">
      <Button
        variant="ghost"
        size="icon-sm"
        title="Save (Ctrl+S / ⌘S)"
        aria-label="Save file"
        :disabled="
          !activeTab ||
          activeTab.readOnly ||
          !activeTab.dirty ||
          activeTab.loading ||
          activeTab.saving ||
          activeTab.busy
        "
        data-testid="editor-save"
        @click="emit('save')"
      >
        <Loader2 v-if="activeTab?.saving" :size="15" class="animate-spin" />
        <Save v-else :size="15" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        title="Reload from disk"
        aria-label="Reload from disk"
        :disabled="!activeTab || activeTab.loading || activeTab.saving || activeTab.busy"
        @click="emit('reload')"
      >
        <RefreshCw :size="15" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        title="Download file"
        aria-label="Download file"
        :disabled="!activeTab"
        @click="emit('download')"
      >
        <Download :size="15" />
      </Button>
      <Button
        variant="ghost"
        size="icon-sm"
        :title="chatOpen ? 'Close chat' : 'Open chat'"
        :aria-label="chatOpen ? 'Close chat' : 'Open chat'"
        :aria-pressed="chatOpen"
        @click="emit('toggle-chat')"
      >
        <MessageSquare :size="15" :class="chatOpen ? 'text-primary' : ''" />
      </Button>
      <SidePanelToggle :open="sidePanelOpen" @toggle="emit('toggle-side-panel')" />
    </div>
  </header>
</template>
