<script setup lang="ts">
import { ref } from 'vue'
import { Upload, Loader2 } from 'lucide-vue-next'
import { sendFilesUpload } from '@/services/socket'
import { useFileExplorerStore } from '@/stores/fileExplorer'

const props = defineProps<{
  workspaceId: string
  targetPath: string
}>()

const emit = defineEmits<{
  uploaded: []
}>()

const isDragOver = ref(false)
const isUploading = ref(false)
const FILE_MAX_SIZE = 10 * 1024 * 1024 // 10 MB

const fileExplorer = useFileExplorerStore()

let dragCounter = 0

function onDragEnter(e: DragEvent): void {
  e.preventDefault()
  dragCounter++
  isDragOver.value = true
}

function onDragLeave(e: DragEvent): void {
  e.preventDefault()
  dragCounter--
  if (dragCounter <= 0) {
    isDragOver.value = false
    dragCounter = 0
  }
}

function onDragOver(e: DragEvent): void {
  e.preventDefault()
}

async function uploadFiles(fileList: FileList): Promise<void> {
  const files = Array.from(fileList)
  if (files.length === 0) return

  isUploading.value = true

  const uploads: Promise<void>[] = []

  for (const file of files) {
    if (file.size > FILE_MAX_SIZE) {
      console.warn(`File "${file.name}" exceeds 10 MB limit`)
      continue
    }

    const buffer = await file.arrayBuffer()
    const bytes = new Uint8Array(buffer)
    let binary = ''
    for (let i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]!)
    }
    const content = btoa(binary)

    const requestId = `upload-${Date.now()}-${file.name}`

    // Register upload so we can await the result
    uploads.push(fileExplorer.trackAndUpload(props.workspaceId, requestId, props.targetPath, file.name, content))

    sendFilesUpload(
      props.workspaceId,
      requestId,
      props.targetPath,
      file.name,
      content,
      false,
    )
  }

  // Wait for all uploads to complete (success or error)
  await Promise.allSettled(uploads)

  isUploading.value = false
  emit('uploaded')
}

async function onDrop(e: DragEvent): Promise<void> {
  e.preventDefault()
  isDragOver.value = false
  dragCounter = 0

  const files = e.dataTransfer?.files
  if (!files || files.length === 0) return
  await uploadFiles(files)
}
</script>

<template>
  <div
    class="relative h-full"
    @dragenter="onDragEnter"
    @dragleave="onDragLeave"
    @dragover="onDragOver"
    @drop="onDrop"
  >
    <slot />

    <!-- Drop overlay -->
    <div
      v-if="isDragOver"
      class="absolute inset-0 z-40 bg-primary/10 border-2 border-dashed border-primary rounded-lg flex items-center justify-center"
    >
      <div class="flex flex-col items-center gap-2 text-primary">
        <Upload :size="24" />
        <span class="text-sm font-medium">Drop files here</span>
      </div>
    </div>

    <!-- Uploading overlay -->
    <div
      v-if="isUploading"
      class="absolute inset-0 z-40 bg-surface/70 rounded-lg flex items-center justify-center"
    >
      <div class="flex flex-col items-center gap-2 text-primary">
        <Loader2 :size="24" class="animate-spin" />
        <span class="text-sm font-medium">Uploading…</span>
      </div>
    </div>
  </div>
</template>
