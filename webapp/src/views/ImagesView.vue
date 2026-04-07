<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useImageStore } from '@/stores/images'
import { usePolling } from '@/composables/usePolling'
import { UiSpinner, UiButton, UiCard, UiCardContent, UiBadge } from '@/components/ui'
import CreateImageArtifactDialog from '@/components/workspaces/CreateImageArtifactDialog.vue'
import CreateWorkspaceFromImageArtifactDialog from '@/components/workspaces/CreateWorkspaceFromImageArtifactDialog.vue'
import { Camera, Copy, Trash2, HardDrive, Calendar, Pencil, Check, X, AlertTriangle, Loader2, WifiOff } from 'lucide-vue-next'
import type { ImageArtifact } from '@/types'

const imageStore = useImageStore()

const deletingId = ref<string | null>(null)
const editingId = ref<string | null>(null)
const editName = ref('')

const capturedImages = computed(() =>
  imageStore.images.filter((entry) => entry.artifact_kind === 'captured'),
)

const hasCreating = computed(() =>
  capturedImages.value.some((image) => image.status === 'creating'),
)

const { start } = usePolling(
  async () => {
    await imageStore.fetchImages()
  },
  computed(() => (hasCreating.value ? 3000 : 15000)),
)

onMounted(() => {
  start()
})

function formatBytes(bytes: number | null): string {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleString()
}

async function handleDelete(imageArtifact: ImageArtifact): Promise<void> {
  if (!confirm(`Delete image "${imageArtifact.name}"? This cannot be undone.`)) return
  deletingId.value = imageArtifact.id
  await imageStore.deleteImageArtifact(imageArtifact.id)
  deletingId.value = null
}

function startRename(imageArtifact: ImageArtifact): void {
  editingId.value = imageArtifact.id
  editName.value = imageArtifact.name
}

function cancelRename(): void {
  editingId.value = null
  editName.value = ''
}

async function confirmRename(imageArtifact: ImageArtifact): Promise<void> {
  const trimmed = editName.value.trim()
  if (!trimmed || trimmed === imageArtifact.name) {
    cancelRename()
    return
  }
  await imageStore.renameImageArtifact(imageArtifact.id, trimmed)
  editingId.value = null
}
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold text-fg">Captured Images</h2>
        <p class="text-sm text-muted-fg mt-1">
          Reusable workspace images captured from your workspaces.
        </p>
      </div>
      <CreateImageArtifactDialog />
    </div>

    <div v-if="imageStore.loading && !imageStore.images.length" class="flex justify-center py-12">
      <UiSpinner :size="24" />
    </div>

    <div
      v-else-if="imageStore.error"
      class="rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-4 py-3 text-sm text-error"
    >
      {{ imageStore.error }}
    </div>

    <div v-else-if="!capturedImages.length" class="py-10 text-center text-sm text-muted-fg">
      No captured images yet.
    </div>

    <div v-else class="grid gap-3">
      <UiCard
        v-for="imageArtifact in capturedImages"
        :key="imageArtifact.id"
        :class="`transition-colors duration-150 hover:border-border-hover${imageArtifact.status === 'failed' ? ' opacity-60' : ''}${imageArtifact.is_deactivated ? ' opacity-60' : ''}${imageArtifact.source_runner_online === false ? ' opacity-60' : ''} border-dashed`"
      >
        <UiCardContent>
          <div class="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div class="flex items-start gap-3 min-w-0">
              <div
                :class="[
                  'flex items-center justify-center w-10 h-10 rounded-[var(--radius-md)] shrink-0',
                  imageArtifact.status === 'creating'
                    ? 'bg-warning-muted text-warning'
                    : imageArtifact.status === 'failed'
                      ? 'bg-error-muted text-error'
                      : 'bg-muted text-muted-fg',
                ]"
              >
                <Loader2 v-if="imageArtifact.status === 'creating'" :size="18" class="animate-spin" />
                <AlertTriangle v-else-if="imageArtifact.status === 'failed'" :size="18" />
                <Camera v-else :size="18" />
              </div>

              <div class="min-w-0 flex-1">
                <div v-if="editingId === imageArtifact.id" class="flex items-center gap-1.5 mb-1">
                    <input
                      v-model="editName"
                      class="text-sm font-medium text-fg bg-bg-subtle border border-border rounded px-2 py-0.5 focus:outline-none focus:ring-1 focus:ring-accent min-w-0 w-full sm:w-48"
                      @keydown.enter="confirmRename(imageArtifact)"
                      @keydown.escape="cancelRename"
                      autofocus
                    />
                    <button class="text-success hover:text-success" @click="confirmRename(imageArtifact)">
                      <Check :size="14" />
                    </button>
                    <button class="text-muted-fg hover:text-fg" @click="cancelRename">
                      <X :size="14" />
                    </button>
                </div>
                <div v-else class="flex items-start gap-2 mb-1">
                  <span class="font-medium text-fg text-sm min-w-0 break-words">{{ imageArtifact.name }}</span>
                  <button
                    v-if="imageArtifact.status !== 'creating' && editingId !== imageArtifact.id"
                    class="text-muted-fg hover:text-fg transition-colors shrink-0"
                    title="Rename image"
                    @click="startRename(imageArtifact)"
                  >
                    <Pencil :size="12" />
                  </button>
                </div>
                <div class="flex flex-wrap items-center gap-1.5 mb-1">
                  <UiBadge v-if="imageArtifact.runtime_type" variant="muted">{{ imageArtifact.runtime_type }}</UiBadge>
                  <UiBadge v-if="imageArtifact.status === 'creating'" variant="warning">Creating…</UiBadge>
                  <UiBadge v-else-if="imageArtifact.status === 'failed'" variant="error">Failed</UiBadge>
                  <UiBadge v-if="imageArtifact.is_deactivated" variant="muted">Deactivated</UiBadge>
                  <UiBadge
                    v-if="imageArtifact.source_runner_online === false"
                    variant="warning"
                    class="inline-flex items-center gap-1"
                  >
                    <WifiOff :size="11" />
                    Runner offline
                  </UiBadge>
                </div>

                <p v-if="imageArtifact.source_definition_name" class="text-xs text-muted-fg mb-1">
                  Built from: {{ imageArtifact.source_definition_name }}
                </p>
                <p v-if="imageArtifact.source_workspace_id" class="text-xs text-muted-fg font-mono mb-2">
                  {{ imageArtifact.source_workspace_id.slice(0, 8) }}…
                </p>

                <div class="flex items-center gap-3 flex-wrap">
                  <span class="inline-flex items-center gap-1 text-xs text-muted-fg">
                    <HardDrive :size="12" />
                    {{ formatBytes(imageArtifact.size_bytes) }}
                  </span>
                  <span class="inline-flex items-center gap-1 text-xs text-muted-fg">
                    <Calendar :size="12" />
                    {{ formatDate(imageArtifact.created_at) }}
                  </span>
                </div>
              </div>
            </div>

            <div class="flex items-center gap-2 w-full sm:w-auto sm:shrink-0">
              <CreateWorkspaceFromImageArtifactDialog
                v-if="imageArtifact.status === 'ready'"
                :image-artifact="imageArtifact"
                :disabled="imageArtifact.source_runner_online === false"
                class="flex-1 sm:flex-none"
              >
                <UiButton
                  variant="outline"
                  size="sm"
                  class="gap-1.5 w-full sm:w-auto justify-center"
                  :disabled="imageArtifact.source_runner_online === false"
                >
                  <Copy :size="14" />
                  {{ imageArtifact.source_runner_online === false ? 'Clone unavailable' : 'Clone Workspace' }}
                </UiButton>
              </CreateWorkspaceFromImageArtifactDialog>

              <UiButton
                variant="ghost"
                size="icon-sm"
                title="Delete image"
                class="text-error hover:text-error"
                :disabled="deletingId === imageArtifact.id"
                @click="handleDelete(imageArtifact)"
              >
                <UiSpinner v-if="deletingId === imageArtifact.id" :size="14" />
                <Trash2 v-else :size="14" />
              </UiButton>
            </div>
          </div>
        </UiCardContent>
      </UiCard>
    </div>
  </div>
</template>
