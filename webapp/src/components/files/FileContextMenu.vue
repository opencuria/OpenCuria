<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import type { FileNode } from '@/types'
import { Download, Copy } from '@lucide/vue'
import { copyToClipboard } from '@/lib/clipboard'

const props = defineProps<{
  node: FileNode
  x: number
  y: number
}>()

const emit = defineEmits<{
  close: []
  download: [path: string]
  copyPath: [path: string]
}>()

const menuRef = ref<HTMLElement | null>(null)
const copyingPath = ref(false)
const controller = new AbortController()

function handleDownload(): void {
  emit('download', props.node.path)
  emit('close')
}

async function handleCopyPath(): Promise<void> {
  if (copyingPath.value || controller.signal.aborted) return
  const path = props.node.path
  copyingPath.value = true
  try {
    const ok = await copyToClipboard(path, 'file path', { signal: controller.signal })
    if (ok && !controller.signal.aborted) emit('copyPath', path)
  } finally {
    copyingPath.value = false
    if (!controller.signal.aborted) emit('close')
  }
}

function handleClickOutside(e: MouseEvent): void {
  const target = e.target as Element | null
  // Interacting with the retry dialog must not abort its pending operation.
  if (copyingPath.value && target?.closest?.('[data-clipboard-dialog]')) return
  if (menuRef.value && !menuRef.value.contains(e.target as Node)) {
    controller.abort()
    emit('close')
  }
}

function handleEscape(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    controller.abort()
    emit('close')
  }
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  document.addEventListener('keydown', handleEscape)
})

onUnmounted(() => {
  controller.abort()
  document.removeEventListener('click', handleClickOutside)
  document.removeEventListener('keydown', handleEscape)
})
</script>

<template>
  <Teleport to="body">
    <div
      ref="menuRef"
      class="fixed z-50 bg-card border border-border rounded-lg shadow-lg py-1 min-w-[160px]"
      :style="{ left: `${x}px`, top: `${y}px` }"
    >
      <button
        class="flex items-center gap-2 w-full px-3 py-1.5 text-sm text-foreground hover:bg-muted transition-colors text-left"
        @click="handleDownload"
      >
        <Download :size="14" />
        Download
      </button>
      <button
        class="flex items-center gap-2 w-full px-3 py-1.5 text-sm text-foreground hover:bg-muted transition-colors text-left"
        :disabled="copyingPath"
        data-testid="file-context-copy-path"
        @click="handleCopyPath"
      >
        <Copy :size="14" />
        Copy path
      </button>
    </div>
  </Teleport>
</template>
