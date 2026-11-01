<script setup lang="ts">
import { inject, ref, watchEffect } from 'vue'
import ChatSidebar from './ChatSidebar.vue'
import { SidebarRail, useSidebar } from '@/components/ui/sidebar'
import { sidebarChatTargetKey, useWorkspaceLayoutStore } from '@/stores/workspaceLayout'

const layout = useWorkspaceLayoutStore()
const { open } = useSidebar()
const chatTarget = inject(sidebarChatTargetKey)!
const isResizing = ref(false)
let startX = 0
let startWidth = 0

watchEffect(() => {
  layout.navigationOpen = open.value
})

function startResize(event: PointerEvent): void {
  if (event.button !== 0) return
  event.preventDefault()
  startX = event.clientX
  startWidth = layout.leftWidth
  isResizing.value = true
  const handle = event.currentTarget as HTMLElement
  handle.setPointerCapture?.(event.pointerId)
}

function resize(event: PointerEvent): void {
  if (isResizing.value) layout.setLeftWidth(startWidth + event.clientX - startX)
}

function stopResize(event: PointerEvent): void {
  if (!isResizing.value) return
  isResizing.value = false
  const handle = event.currentTarget as HTMLElement
  if (handle.hasPointerCapture?.(event.pointerId)) handle.releasePointerCapture(event.pointerId)
  layout.persistLeftWidth()
}

function resizeWithKeyboard(event: KeyboardEvent): void {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  event.preventDefault()
  layout.setLeftWidth(layout.leftWidth + (event.key === 'ArrowRight' ? 16 : -16))
  layout.persistLeftWidth()
}
</script>

<template>
  <div
    class="relative shrink-0"
    :class="{
      'select-none [&_[data-slot=sidebar-gap]]:transition-none [&_[data-slot=sidebar-container]]:transition-none':
        isResizing,
    }"
    :style="{ '--sidebar-width': `${layout.leftWidth}px` }"
  >
    <div v-show="!layout.leftChatDocked">
      <ChatSidebar>
        <template #rail><SidebarRail v-if="!open" /></template>
      </ChatSidebar>
    </div>
    <aside
      v-show="layout.leftChatDocked"
      class="relative flex h-svh min-h-0 flex-col border-r border-border bg-background"
      :style="{ width: `${layout.leftWidth}px` }"
      aria-label="Docked chat"
      data-testid="workspace-left-sidebar"
    >
      <div
        ref="chatTarget"
        class="relative flex min-h-0 flex-1 flex-col"
        data-testid="workspace-left-chat-target"
      />
    </aside>
    <div
      v-if="open || layout.leftChatDocked"
      role="separator"
      tabindex="0"
      aria-orientation="vertical"
      aria-label="Resize left sidebar"
      :aria-valuemin="layout.leftMinimum"
      :aria-valuemax="layout.leftMaxWidth"
      :aria-valuenow="layout.leftWidth"
      title="Drag to resize sidebar"
      class="absolute -right-0.5 inset-y-0 z-30 hidden w-1 cursor-col-resize touch-none hover:bg-primary focus-visible:bg-primary focus-visible:outline-none md:block"
      :class="{ 'bg-primary': isResizing }"
      data-testid="left-sidebar-resize-handle"
      @pointerdown="startResize"
      @pointermove="resize"
      @pointerup="stopResize"
      @pointercancel="stopResize"
      @lostpointercapture="stopResize"
      @keydown="resizeWithKeyboard"
      @dblclick="layout.resetLeftWidth()"
    />
  </div>
</template>
