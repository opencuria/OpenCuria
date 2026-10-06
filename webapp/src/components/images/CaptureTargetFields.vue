<script setup lang="ts">
/**
 * "Save as" fields shared by both capture dialogs: a new image or the next
 * version of an existing image on the same runner, plus a short message.
 */
import { computed, onMounted, watch } from 'vue'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { NEW_IMAGE } from '@/lib/imageVersions'
import { useImageStore } from '@/stores/images'
import type { ImageArtifactCreateIn, Workspace } from '@/types'

const props = defineProps<{
  workspace: Workspace | null
  disabled?: boolean
}>()

const target = defineModel<string>('target', { default: NEW_IMAGE })
const name = defineModel<string>('name', { default: '' })
const message = defineModel<string>('message', { default: '' })

const imageStore = useImageStore()

const candidates = computed(() =>
  imageStore.capturedImages.filter(
    (image) =>
      image.status === 'active' &&
      !!props.workspace &&
      image.runner_id === props.workspace.runner_id &&
      !image.versions.some((version) => version.status === 'capturing'),
  ),
)

const nextVersion = (imageId: string): number => {
  const image = imageStore.capturedImages.find((item) => item.id === imageId)
  const highest = Math.max(0, ...(image?.versions ?? []).map((v) => v.version ?? 0))
  return highest + 1
}

function preselect(): void {
  const base = props.workspace?.base_image
  const own =
    base?.line_kind === 'captured'
      ? candidates.value.find((image) => image.id === base.line_id)
      : undefined
  target.value = own ? own.id : NEW_IMAGE
}

onMounted(async () => {
  if (!imageStore.capturedImages.length) await imageStore.fetchCapturedImages()
  preselect()
})
watch(() => props.workspace?.id, preselect)

const isValid = computed(() =>
  target.value === NEW_IMAGE
    ? name.value.trim().length > 0
    : candidates.value.some((image) => image.id === target.value),
)

function payload(): ImageArtifactCreateIn {
  return target.value === NEW_IMAGE
    ? { name: name.value.trim(), message: message.value.trim() }
    : { captured_image_id: target.value, message: message.value.trim() }
}

defineExpose({ isValid, payload })
</script>

<template>
  <div class="flex flex-col gap-4">
    <div>
      <label class="text-sm font-medium text-foreground mb-1.5 block">Save as</label>
      <Select v-model="target" :disabled="disabled">
        <SelectTrigger data-testid="capture-target-select">
          <SelectValue placeholder="New image" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem :value="NEW_IMAGE">New image</SelectItem>
          <template v-if="candidates.length">
            <SelectSeparator />
            <SelectItem v-for="image in candidates" :key="image.id" :value="image.id">
              {{ image.name }} · v{{ nextVersion(image.id) }}
            </SelectItem>
          </template>
        </SelectContent>
      </Select>
    </div>
    <div v-if="target === NEW_IMAGE">
      <label class="text-sm font-medium text-foreground mb-1.5 block">Image name</label>
      <Input
        v-model="name"
        :disabled="disabled"
        placeholder="e.g. node-dev"
        data-testid="capture-name-input"
      />
    </div>
    <div>
      <label class="text-sm font-medium text-foreground mb-1.5 block">What changed?</label>
      <Textarea
        v-model="message"
        :disabled="disabled"
        maxlength="500"
        placeholder="Optional, e.g. Installed Playwright browsers"
        data-testid="capture-message-input"
      />
    </div>
  </div>
</template>
