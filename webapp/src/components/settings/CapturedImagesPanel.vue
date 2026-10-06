<!--
  CapturedImagesPanel — versioned captured images (one row per image).
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
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import ImageDeletionDialog from '@/components/images/ImageDeletionDialog.vue'
import ImageVersionList from '@/components/images/ImageVersionList.vue'
import SettingsSection from './SettingsSection.vue'
import SettingsRow from './SettingsRow.vue'
import CreateImageArtifactDialog from '@/components/workspaces/CreateImageArtifactDialog.vue'
import CreateWorkspaceFromImageArtifactDialog from '@/components/workspaces/CreateWorkspaceFromImageArtifactDialog.vue'
import {
  Camera,
  ChevronDown,
  Copy,
  Trash2,
  HardDrive,
  Pencil,
  Check,
  X,
  Loader2,
  WifiOff,
} from '@lucide/vue'
import { cn } from '@/lib/utils'
import { storageBytes } from '@/composables/useRunnerStorage'
import type { CapturedImage, ImageArtifact } from '@/types'
import type { DeletionTarget } from '@/types/runnerStorage'

const imageStore = useImageStore()

const pendingDelete = ref<{ target: DeletionTarget; name: string } | null>(null)
const editingId = ref<string | null>(null)
const editName = ref('')

const images = computed(() => imageStore.capturedImages)

function latestOf(image: CapturedImage): ImageArtifact | undefined {
  return image.versions.find((version) => version.id === image.latest_id)
}

function isCapturing(image: CapturedImage): boolean {
  return image.versions.some((version) => version.status === 'capturing')
}

const hasCapturing = computed(() => images.value.some(isCapturing))

const { start } = usePolling(
  async () => {
    await imageStore.fetchCapturedImages()
  },
  computed(() => (hasCapturing.value ? 3000 : 15000)),
)

onMounted(() => {
  start()
})

function deleteImage(image: CapturedImage): void {
  pendingDelete.value = {
    target: { target_type: 'captured_image', target_id: image.id },
    name: image.name,
  }
}

function deleteVersion(image: CapturedImage, version: ImageArtifact): void {
  pendingDelete.value = {
    target: { target_type: 'image', target_id: version.id },
    name: `${image.name} · v${version.version}`,
  }
}

function startRename(image: CapturedImage): void {
  editingId.value = image.id
  editName.value = image.name
}

function cancelRename(): void {
  editingId.value = null
  editName.value = ''
}

async function confirmRename(image: CapturedImage): Promise<void> {
  const trimmed = editName.value.trim()
  if (!trimmed || trimmed === image.name) {
    cancelRename()
    return
  }
  await imageStore.renameCapturedImage(image.id, trimmed)
  editingId.value = null
}
</script>

<template>
  <div class="space-y-6">
    <SettingsSection
      description="Versioned images captured from your workspaces. New workspaces always use the latest version."
    >
      <template #actions>
        <CreateImageArtifactDialog />
      </template>

      <div v-if="imageStore.loading && !images.length" class="flex justify-center py-12">
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="imageStore.error"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        {{ imageStore.error }}
      </div>

      <div v-else-if="!images.length" class="overflow-hidden rounded-lg border border-border bg-card">
        <EmptyState
          :icon="Camera"
          title="No captured images yet"
          description="Capture a QEMU workspace to reuse its filesystem for new workspaces."
        />
      </div>

      <div v-else class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
        <Collapsible v-for="image in images" :key="image.id" data-testid="captured-image-row">
          <SettingsRow
            :class="cn(image.status !== 'active' || !image.runner_online ? 'opacity-80' : undefined)"
          >
            <template #icon>
              <Loader2 v-if="isCapturing(image)" :size="16" class="animate-spin" />
              <Camera v-else :size="16" />
            </template>

            <div class="min-w-0 space-y-1.5">
              <div v-if="editingId === image.id" class="flex items-center gap-1.5">
                <Input
                  v-model="editName"
                  class="h-8 max-w-xs"
                  @keydown.enter="confirmRename(image)"
                  @keydown.escape="cancelRename"
                />
                <Button
                  variant="ghost"
                  size="icon-sm"
                  class="text-success hover:text-success"
                  @click="confirmRename(image)"
                >
                  <Check />
                </Button>
                <Button variant="ghost" size="icon-sm" @click="cancelRename">
                  <X />
                </Button>
              </div>
              <div v-else class="flex items-center gap-2">
                <span class="min-w-0 text-sm font-medium break-words text-foreground">
                  {{ image.name }}
                </span>
                <Button
                  v-if="image.status === 'active'"
                  variant="ghost"
                  size="icon-sm"
                  title="Rename image"
                  @click="startRename(image)"
                >
                  <Pencil />
                </Button>
              </div>

              <div class="flex flex-wrap items-center gap-1.5">
                <Badge v-if="image.latest_version" variant="secondary">
                  v{{ image.latest_version }}
                </Badge>
                <Badge v-else variant="destructive">No ready version</Badge>
                <Badge v-if="isCapturing(image)" variant="outline">Capturing…</Badge>
                <Badge v-if="image.status === 'pending_deletion'" variant="destructive">
                  Deleting when unused
                </Badge>
                <Badge
                  v-if="!image.runner_online"
                  variant="outline"
                  class="inline-flex items-center gap-1"
                >
                  <WifiOff :size="11" />
                  Runner offline
                </Badge>
              </div>

              <CollapsibleTrigger
                class="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground [&[data-state=open]>svg]:rotate-180"
                data-testid="captured-image-versions-toggle"
              >
                <HardDrive :size="12" />
                {{ image.versions.length }}
                {{ image.versions.length === 1 ? 'version' : 'versions' }} ·
                {{ storageBytes(image.total_size_bytes) }}
                <template v-if="image.workspace_count">
                  · used by {{ image.workspace_count }}
                  {{ image.workspace_count === 1 ? 'workspace' : 'workspaces' }}
                </template>
                <ChevronDown :size="12" class="transition-transform" />
              </CollapsibleTrigger>
            </div>

            <template #actions>
              <CreateWorkspaceFromImageArtifactDialog
                v-if="latestOf(image)"
                :image-artifact="latestOf(image)!"
                :captured-image-id="image.id"
                :disabled="!image.runner_online"
              >
                <Button variant="outline" size="sm" :disabled="!image.runner_online">
                  <Copy />
                  New workspace
                </Button>
              </CreateWorkspaceFromImageArtifactDialog>
              <Button
                variant="ghost"
                size="icon-sm"
                title="Delete image"
                aria-label="Delete image or inspect pending request"
                class="text-destructive hover:text-destructive"
                @click="deleteImage(image)"
              >
                <Trash2 />
              </Button>
            </template>
          </SettingsRow>
          <CollapsibleContent class="px-4 pb-3">
            <ImageVersionList
              :versions="image.versions"
              :can-delete="image.status === 'active'"
              @delete="(version) => deleteVersion(image, version)"
            />
          </CollapsibleContent>
        </Collapsible>
      </div>
    </SettingsSection>

    <ImageDeletionDialog
      :target="pendingDelete?.target ?? null"
      :name="pendingDelete?.name"
      @close="pendingDelete = null"
      @requested="imageStore.fetchCapturedImages"
    />
  </div>
</template>
