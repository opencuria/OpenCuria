<!--
  CapturedImagesPanel — captured workspace images list.
  Polls (3s while capturing, otherwise 15s).
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useImageStore } from '@/stores/images'
import { usePolling } from '@/composables/usePolling'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import SettingsSection from './SettingsSection.vue'
import SettingsRow from './SettingsRow.vue'
import CreateImageArtifactDialog from '@/components/workspaces/CreateImageArtifactDialog.vue'
import CreateWorkspaceFromImageArtifactDialog from '@/components/workspaces/CreateWorkspaceFromImageArtifactDialog.vue'
import {
  Camera,
  Copy,
  Trash2,
  HardDrive,
  Calendar,
  Pencil,
  Check,
  X,
  AlertTriangle,
  Loader2,
  WifiOff,
} from '@lucide/vue'
import { cn, formatDate } from '@/lib/utils'
import type { ImageArtifact } from '@/types'

const imageStore = useImageStore()

const deletingId = ref<string | null>(null)
const pendingDelete = ref<ImageArtifact | null>(null)
const editingId = ref<string | null>(null)
const editName = ref('')

const capturedImages = computed(() =>
  imageStore.images.filter((entry) => entry.artifact_kind === 'captured'),
)

function isCaptureInProgress(imageArtifact: ImageArtifact): boolean {
  return imageArtifact.status === 'creating' || imageArtifact.status === 'capturing'
}

const hasCreating = computed(() => capturedImages.value.some((image) => isCaptureInProgress(image)))

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

function iconClassFor(imageArtifact: ImageArtifact): string {
  if (isCaptureInProgress(imageArtifact)) return 'bg-warning-muted text-warning'
  if (imageArtifact.status === 'failed') return 'bg-destructive/10 text-destructive'
  return ''
}

function requestDelete(imageArtifact: ImageArtifact): void {
  pendingDelete.value = imageArtifact
}

async function confirmDelete(): Promise<void> {
  if (!pendingDelete.value) return
  deletingId.value = pendingDelete.value.id
  await imageStore.deleteImageArtifact(pendingDelete.value.id)
  deletingId.value = null
  pendingDelete.value = null
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
    <SettingsSection description="Reusable workspace images captured from your workspaces.">
      <template #actions>
        <CreateImageArtifactDialog />
      </template>

      <div v-if="imageStore.loading && !imageStore.images.length" class="flex justify-center py-12">
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="imageStore.error"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        {{ imageStore.error }}
      </div>

      <div
        v-else-if="!capturedImages.length"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState
          :icon="Camera"
          title="No captured images yet"
          description="Capture a QEMU workspace to reuse its filesystem for new workspaces."
        />
      </div>

      <div
        v-else
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <SettingsRow
          v-for="imageArtifact in capturedImages"
          :key="imageArtifact.id"
          :icon-class="iconClassFor(imageArtifact)"
          :class="
            cn(
              imageArtifact.status === 'failed' ||
                imageArtifact.is_deactivated ||
                imageArtifact.source_runner_online === false
                ? 'opacity-80'
                : undefined,
            )
          "
        >
          <template #icon>
            <Loader2 v-if="isCaptureInProgress(imageArtifact)" :size="16" class="animate-spin" />
            <AlertTriangle v-else-if="imageArtifact.status === 'failed'" :size="16" />
            <Camera v-else :size="16" />
          </template>

          <div class="min-w-0 space-y-1.5">
            <div v-if="editingId === imageArtifact.id" class="flex items-center gap-1.5">
              <Input
                v-model="editName"
                class="h-8 max-w-xs"
                @keydown.enter="confirmRename(imageArtifact)"
                @keydown.escape="cancelRename"
              />
              <Button
                variant="ghost"
                size="icon-sm"
                class="text-success hover:text-success"
                @click="confirmRename(imageArtifact)"
              >
                <Check />
              </Button>
              <Button variant="ghost" size="icon-sm" @click="cancelRename">
                <X />
              </Button>
            </div>
            <div v-else class="flex items-center gap-2">
              <span class="min-w-0 text-sm font-medium break-words text-foreground">{{
                imageArtifact.name
              }}</span>
              <Button
                v-if="!isCaptureInProgress(imageArtifact)"
                variant="ghost"
                size="icon-sm"
                :disabled="['pending_deletion', 'deleting'].includes(imageArtifact.status)"
                title="Rename image"
                @click="startRename(imageArtifact)"
              >
                <Pencil />
              </Button>
            </div>

            <div class="flex flex-wrap items-center gap-1.5">
              <Badge v-if="imageArtifact.runtime_type" variant="secondary">{{
                imageArtifact.runtime_type === 'qemu' ? 'QEMU' : imageArtifact.runtime_type
              }}</Badge>
              <Badge v-if="isCaptureInProgress(imageArtifact)" variant="outline">Creating…</Badge>
              <Badge v-else-if="imageArtifact.status === 'failed'" variant="destructive">Failed</Badge>
              <Badge
                v-else-if="imageArtifact.status === 'pending_deletion'"
                variant="destructive"
              >Pending deletion</Badge>
              <Badge
                v-else-if="imageArtifact.status === 'deleting'"
                variant="destructive"
                class="inline-flex items-center gap-1"
              >
                <Loader2 :size="11" class="animate-spin" />
                Deleting
              </Badge>
              <Badge v-else-if="imageArtifact.status === 'delete_failed'" variant="destructive">
                Delete failed
              </Badge>
              <Badge v-if="imageArtifact.is_deactivated" variant="secondary">Deactivated</Badge>
              <Badge
                v-if="imageArtifact.source_runner_online === false"
                variant="outline"
                class="inline-flex items-center gap-1"
              >
                <WifiOff :size="11" />
                Runner offline
              </Badge>
            </div>

            <p
              v-if="imageArtifact.source_definition_name"
              class="text-xs text-muted-foreground"
            >
              Built from: {{ imageArtifact.source_definition_name }}
            </p>
            <p
              v-if="imageArtifact.source_workspace_id"
              class="font-mono text-xs text-muted-foreground"
            >
              {{ imageArtifact.source_workspace_id.slice(0, 8) }}…
            </p>
            <div class="flex flex-wrap items-center gap-3">
              <span class="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <HardDrive :size="12" />
                {{ formatBytes(imageArtifact.size_bytes) }}
              </span>
              <span class="inline-flex items-center gap-1 text-xs text-muted-foreground">
                <Calendar :size="12" />
                {{ formatDate(imageArtifact.created_at) }}
              </span>
            </div>
          </div>

          <template #actions>
            <CreateWorkspaceFromImageArtifactDialog
              v-if="imageArtifact.status === 'ready'"
              :image-artifact="imageArtifact"
              :disabled="imageArtifact.source_runner_online === false"
            >
              <Button
                variant="outline"
                size="sm"
                :disabled="imageArtifact.source_runner_online === false"
              >
                <Copy />
                {{
                  imageArtifact.source_runner_online === false
                    ? 'Clone unavailable'
                    : 'Clone Workspace'
                }}
              </Button>
            </CreateWorkspaceFromImageArtifactDialog>

            <Button
              variant="ghost"
              size="icon-sm"
              title="Delete image"
              class="text-destructive hover:text-destructive"
              :disabled="
                deletingId === imageArtifact.id ||
                ['pending_deletion', 'deleting'].includes(imageArtifact.status)
              "
              @click="requestDelete(imageArtifact)"
            >
              <LoadingSpinner v-if="deletingId === imageArtifact.id" :size="14" />
              <Trash2 v-else />
            </Button>
          </template>
        </SettingsRow>
      </div>
    </SettingsSection>

    <Dialog
      :open="pendingDelete !== null"
      @update:open="(open) => !open && (pendingDelete = null)"
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete image</DialogTitle>
          <DialogDescription>
            Delete {{ pendingDelete?.name }}? This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" @click="pendingDelete = null">Cancel</Button>
          <Button variant="destructive" @click="confirmDelete">Delete</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
