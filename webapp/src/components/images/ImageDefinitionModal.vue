<script setup lang="ts">
/**
 * Create or edit an image recipe: name, base image and packages up front,
 * environment and init script under Advanced. Saves only on submit.
 */
import { computed, ref, watch } from 'vue'
import { ChevronDown } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import type { ImageDefinition, ImageDefinitionInput } from '@/types'
import {
  BASE_DISTRO_PRESETS,
  envVarsTextValid,
  formBaseDistro,
  imageDefinitionForm,
  imageDefinitionPayload,
  isBaseDistroSupported,
  isImageDefinitionFormValid,
  type ImageDefinitionForm,
} from './imageDefinitionLifecycle'

const props = withDefaults(
  defineProps<{
    open: boolean
    imageDefinition?: ImageDefinition | null
    saving?: boolean
    error?: string | null
  }>(),
  {
    imageDefinition: null,
    saving: false,
    error: null,
  },
)

const emit = defineEmits<{
  'update:open': [value: boolean]
  saved: [payload: ImageDefinitionInput]
}>()

const form = ref<ImageDefinitionForm>(imageDefinitionForm())
const advancedOpen = ref(false)

const isValid = computed(() => isImageDefinitionFormValid(form.value))
const envValid = computed(() => envVarsTextValid(form.value.envVars))
const customDistroInvalid = computed(
  () =>
    form.value.baseDistroPreset === 'custom' &&
    form.value.customBaseDistro.trim() !== '' &&
    !isBaseDistroSupported(formBaseDistro(form.value)),
)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    form.value = imageDefinitionForm(props.imageDefinition)
    // Never hide settings a recipe already uses.
    advancedOpen.value = Boolean(form.value.envVars || form.value.customInitScript)
  },
  { immediate: true },
)

/** "Ubuntu 24.04" for "ubuntu:24.04". */
function distroLabel(value: string): string {
  return value.replace(/^ubuntu:/, 'Ubuntu ')
}

function handleSave(): void {
  if (isValid.value && !props.saving) emit('saved', imageDefinitionPayload(form.value))
}

function setOpen(value: boolean): void {
  if (!props.saving) emit('update:open', value)
}
</script>

<template>
  <Dialog :open="open" @update:open="setOpen">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>{{
          imageDefinition ? 'Edit Image Definition' : 'New Image Definition'
        }}</DialogTitle>
        <DialogDescription class="sr-only">
          Name, base image and packages of the image recipe.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="error" class="mb-4 text-sm text-destructive" role="alert">{{ error }}</p>
        <form id="image-definition-form" @submit.prevent="handleSave">
          <fieldset :disabled="saving" class="min-w-0 space-y-4">
            <div class="space-y-1.5">
              <Label for="image-definition-name">Name</Label>
              <Input id="image-definition-name" v-model="form.name" placeholder="Python dev" />
            </div>

            <div class="space-y-1.5">
              <Label for="image-definition-description">Description</Label>
              <Input
                id="image-definition-description"
                v-model="form.description"
                placeholder="Optional"
              />
            </div>

            <div class="grid gap-4 sm:grid-cols-2">
              <div class="space-y-1.5">
                <Label for="image-definition-base">Base image</Label>
                <Select v-model="form.baseDistroPreset">
                  <SelectTrigger id="image-definition-base" class="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem v-for="preset in BASE_DISTRO_PRESETS" :key="preset" :value="preset">
                      {{ distroLabel(preset) }}
                    </SelectItem>
                    <SelectItem value="custom">Other…</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div v-if="form.baseDistroPreset === 'custom'" class="space-y-1.5">
                <Label for="image-definition-custom-base">Ubuntu version</Label>
                <Input
                  id="image-definition-custom-base"
                  v-model="form.customBaseDistro"
                  class="font-mono"
                  placeholder="ubuntu:25.04"
                  :aria-invalid="customDistroInvalid || undefined"
                />
                <p v-if="customDistroInvalid" class="text-xs text-destructive">
                  Use ubuntu:&lt;version&gt;.
                </p>
              </div>
            </div>

            <div class="space-y-1.5">
              <Label for="image-definition-packages">Packages</Label>
              <Input
                id="image-definition-packages"
                v-model="form.packages"
                placeholder="python3, nodejs, ffmpeg"
              />
            </div>

            <Collapsible v-model:open="advancedOpen">
              <CollapsibleTrigger
                class="inline-flex items-center gap-1 rounded-sm text-sm font-medium text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                data-testid="image-definition-advanced"
              >
                Advanced
                <ChevronDown
                  class="size-4 transition-transform"
                  :class="{ 'rotate-180': advancedOpen }"
                  aria-hidden="true"
                />
              </CollapsibleTrigger>
              <CollapsibleContent class="space-y-4 pt-4">
                <div class="space-y-1.5">
                  <Label for="image-definition-env">Environment variables</Label>
                  <Textarea
                    id="image-definition-env"
                    v-model="form.envVars"
                    :rows="3"
                    class="font-mono"
                    placeholder="KEY=value"
                    :aria-invalid="!envValid || undefined"
                  />
                  <p v-if="!envValid" class="text-xs text-destructive">One KEY=value per line.</p>
                </div>
                <div class="space-y-1.5">
                  <Label for="image-definition-script">Init script</Label>
                  <Textarea
                    id="image-definition-script"
                    v-model="form.customInitScript"
                    :rows="6"
                    class="font-mono"
                    placeholder="#!/bin/bash"
                  />
                </div>
              </CollapsibleContent>
            </Collapsible>
          </fieldset>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="saving" @click="setOpen(false)">
          Cancel
        </Button>
        <Button type="submit" form="image-definition-form" :disabled="saving || !isValid">
          {{ saving ? 'Saving…' : imageDefinition ? 'Save' : 'Create' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
