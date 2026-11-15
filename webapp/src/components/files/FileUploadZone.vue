<script setup lang="ts">
import { ref } from 'vue'
import { Upload, Loader2, AlertTriangle } from '@lucide/vue'
import { sendFilesUpload } from '@/services/socket'
import { useFileExplorerStore } from '@/stores/fileExplorer'
import { useNotificationStore } from '@/stores/notifications'

const props = defineProps<{
  workspaceId: string
  targetPath: string
}>()

const emit = defineEmits<{
  uploaded: []
}>()

const isDragOver = ref(false)
const isUploading = ref(false)
const uploadError = ref<string | null>(null)
const FILE_MAX_SIZE = 10 * 1024 * 1024 // 10 MB

const fileExplorer = useFileExplorerStore()

let dragCounter = 0

/** Non-colliding upload request id: crypto.randomUUID when available. */
let uploadFallbackCounter = 0
function nextUploadRequestId(fileName: string): string {
  try {
    const uuid = globalThis.crypto?.randomUUID?.()
    if (typeof uuid === 'string' && uuid.length > 0) return `upload-${uuid}`
  } catch {
    // fall through to the timestamp+counter fallback below
  }
  uploadFallbackCounter += 1
  return `upload-${Date.now()}-${uploadFallbackCounter}-${fileName}`
}

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
  uploadError.value = null
  const notify = useNotificationStore()

  const uploads: Promise<void>[] = []

  for (const file of files) {
    if (file.size > FILE_MAX_SIZE) {
      const message = `File "${file.name}" exceeds the 10 MB upload limit and was skipped.`
      uploadError.value = message
      notify.error('Upload failed', message)
      continue
    }

    const buffer = await file.arrayBuffer()
    const bytes = new Uint8Array(buffer)
    let binary = ''
    for (let i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]!)
    }
    const content = btoa(binary)

    const requestId = nextUploadRequestId(file.name)

    // Register first so a synchronous sendFilesUpload throw (oversize
    // cap) still resolves through the tracked promise instead of
    // hanging the overlay for 30 s: fail the pending upload, then
    // surface the error visibly.
    const tracked = fileExplorer.trackAndUpload(props.workspaceId, requestId, props.targetPath, file.name, content)
    uploads.push(tracked)
    try {
      sendFilesUpload(
        props.workspaceId,
        requestId,
        props.targetPath,
        file.name,
        content,
        false,
      )
    } catch (err) {
      const message = err instanceof Error ? err.message : 'The file could not be uploaded.'
      fileExplorer.failUpload(requestId, message)
    }
  }

  // Wait for all uploads to complete (success or error); the pending
  // rejections are consumed here so the overlay always clears.
  const results = await Promise.allSettled(uploads)
  for (const result of results) {
    if (result.status === 'rejected') {
      const message =
        result.reason instanceof Error ? result.reason.message : 'The file could not be uploaded.'
      uploadError.value = message
    }
  }

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
      class="absolute inset-0 z-40 bg-card/70 rounded-lg flex items-center justify-center"
    >
      <div class="flex flex-col items-center gap-2 text-primary">
        <Loader2 :size="24" class="animate-spin" />
        <span class="text-sm font-medium">Uploading…</span>
      </div>
    </div>
  </div>
</template>
