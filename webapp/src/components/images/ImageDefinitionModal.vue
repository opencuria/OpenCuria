<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
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
import { Textarea } from '@/components/ui/textarea'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { ImageDefinition } from '@/types'
import {
  imageDefinitionPayload,
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
  saved: [payload: Partial<ImageDefinition>]
}>()

const name = ref('')
const description = ref('')
const activeTab = ref<'definition' | 'advanced'>('definition')
const baseDistroPreset = ref('ubuntu:22.04')
const customBaseDistro = ref('')
const packages = ref('')
const envVars = ref('')
const customInitScript = ref('')

const distroOptions = [
  { value: 'ubuntu:22.04', label: 'ubuntu:22.04' },
  { value: 'ubuntu:24.04', label: 'ubuntu:24.04' },
  { value: '__custom__', label: 'Custom...' },
]

const title = computed(() =>
  props.imageDefinition ? 'Edit Image Definition' : 'Create Image Definition',
)
/** The modal's fields in the shared form shape. */
const form = computed<ImageDefinitionForm>(() => ({
  name: name.value,
  description: description.value,
  baseDistroPreset: baseDistroPreset.value === '__custom__' ? 'custom' : baseDistroPreset.value,
  customBaseDistro: customBaseDistro.value,
  packages: packages.value,
  envVars: envVars.value,
  customInitScript: customInitScript.value,
}))
const isValid = computed(() => isImageDefinitionFormValid(form.value))

function payload(): Partial<ImageDefinition> {
  return imageDefinitionPayload(form.value)
}

watch(
  () => props.open,
  (open) => {
    if (!open) return
    const src = props.imageDefinition
    activeTab.value = 'definition'
    name.value = src?.name || ''
    description.value = src?.description || ''
    const currentBase = src?.base_distro || 'ubuntu:22.04'
    const known = distroOptions.find((opt) => opt.value === currentBase)
    baseDistroPreset.value = known ? currentBase : '__custom__'
    customBaseDistro.value = known ? '' : currentBase
    packages.value = (src?.packages || []).join(', ')
    envVars.value = Object.entries(src?.env_vars || {})
      .map(([k, v]) => `${k}=${v}`)
      .join('\n')
    customInitScript.value = src?.custom_init_script || ''
  },
  { immediate: true },
)

function handleSave(): void {
  if (isValid.value && !props.saving) emit('saved', payload())
}

function setOpen(value: boolean): void {
  if (!props.saving) emit('update:open', value)
}
</script>

<template>
  <Dialog :open="open" @update:open="setOpen">
    <DialogContent class="sm:max-w-lg">
      <DialogHeader>
        <DialogTitle>{{ title }}</DialogTitle>
        <DialogDescription>
          Define base distro, packages and advanced build customization.
        </DialogDescription>
      </DialogHeader>

      <DialogBody>
        <p v-if="error" class="text-sm text-destructive" role="alert">{{ error }}</p>
        <form id="image-definition-form" class="space-y-4" @submit.prevent="handleSave">
          <fieldset :disabled="saving" class="contents">
            <div class="rounded-md border border-border bg-muted/50 p-1">
              <div class="grid grid-cols-2 gap-1">
                <Button
                  type="button"
                  size="sm"
                  :variant="activeTab === 'definition' ? 'secondary' : 'ghost'"
                  @click="activeTab = 'definition'"
                >
                  Definition
                </Button>
                <Button
                  type="button"
                  size="sm"
                  :variant="activeTab === 'advanced' ? 'secondary' : 'ghost'"
                  @click="activeTab = 'advanced'"
                >
                  Advanced / Custom Build
                </Button>
              </div>
            </div>

            <template v-if="activeTab === 'definition'">
              <div>
                <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
                <Input v-model="name" placeholder="Python Dev Environment" />
              </div>

              <div>
                <label class="text-sm font-medium text-foreground mb-1.5 block">Description</label>
                <Textarea v-model="description" :rows="2" />
              </div>

              <div class="grid gap-3">
                <div>
                  <label class="text-sm font-medium text-foreground mb-1.5 block"
                    >Base Distro</label
                  >
                  <Select v-model="baseDistroPreset">
                    <SelectTrigger>
                      <SelectValue placeholder="Select distro" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem v-for="opt in distroOptions" :key="opt.value" :value="opt.value">
                        {{ opt.label }}
                      </SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <p class="text-xs text-muted-foreground -mt-1">
                QEMU image builds currently support Ubuntu cloud images only.
              </p>

              <div v-if="baseDistroPreset === '__custom__'">
                <label class="text-sm font-medium text-foreground mb-1.5 block"
                  >Custom Base Distro</label
                >
                <Input v-model="customBaseDistro" placeholder="e.g. ubuntu:22.04" />
              </div>

              <div>
                <label class="text-sm font-medium text-foreground mb-1.5 block"
                  >Pre-installed Packages</label
                >
                <Input v-model="packages" placeholder="python3, nodejs, ffmpeg" />
              </div>

              <div>
                <label class="text-sm font-medium text-foreground mb-1.5 block"
                  >Baked Environment Variables</label
                >
                <Textarea v-model="envVars" :rows="4" placeholder="NODE_ENV=production" />
              </div>
            </template>

            <template v-else>
              <p class="text-xs text-muted-foreground">
                The base distro and packages above are applied first. Use this section for advanced
                customization.
              </p>
              <div>
                <label class="text-sm font-medium text-foreground mb-1.5 block"
                  >Custom Init Script</label
                >
                <Textarea
                  v-model="customInitScript"
                  :rows="8"
                  placeholder="#!/bin/bash\napt-get install -y ffmpeg"
                />
              </div>
            </template>
          </fieldset>
        </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="saving" @click="setOpen(false)"
          >Cancel</Button
        >
        <Button type="submit" form="image-definition-form" :disabled="saving || !isValid">{{
          saving ? 'Saving…' : 'Save'
        }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
