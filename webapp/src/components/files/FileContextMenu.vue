<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import type { FileNode } from '@/types'
import { Download, Copy } from 'lucide-vue-next'

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

function handleDownload(): void {
  emit('download', props.node.path)
  emit('close')
}

function handleCopyPath(): void {
  navigator.clipboard.writeText(props.node.path)
  emit('copyPath', props.node.path)
  emit('close')
}

function handleClickOutside(e: MouseEvent): void {
  if (menuRef.value && !menuRef.value.contains(e.target as Node)) {
    emit('close')
  }
}

function handleEscape(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    emit('close')
  }
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  document.addEventListener('keydown', handleEscape)
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
  document.removeEventListener('keydown', handleEscape)
})
</script>

<template>
  <Teleport to="body">
    <div
      ref="menuRef"
      class="fixed z-50 bg-surface border border-border rounded-lg shadow-lg py-1 min-w-[160px]"
      :style="{ left: `${x}px`, top: `${y}px` }"
    >
      <button
        class="flex items-center gap-2 w-full px-3 py-1.5 text-sm text-fg hover:bg-muted transition-colors text-left"
        @click="handleDownload"
      >
        <Download :size="14" />
        Download
      </button>
      <button
        class="flex items-center gap-2 w-full px-3 py-1.5 text-sm text-fg hover:bg-muted transition-colors text-left"
        @click="handleCopyPath"
      >
        <Copy :size="14" />
        Copy path
      </button>
    </div>
  </Teleport>
</template>
