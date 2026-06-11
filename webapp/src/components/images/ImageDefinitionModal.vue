<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
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

const props = withDefaults(
  defineProps<{
    open: boolean
    imageDefinition?: ImageDefinition | null
  }>(),
  {
    imageDefinition: null,
  },
)

const emit = defineEmits<{
  'update:open': [value: boolean]
  saved: [payload: Partial<ImageDefinition>]
}>()

const name = ref('')
const description = ref('')
const activeTab = ref<'definition' | 'advanced'>('definition')
const runtimeType = ref<'docker' | 'qemu'>('docker')
const baseDistroPreset = ref('ubuntu:22.04')
const customBaseDistro = ref('')
const packages = ref('')
const envVars = ref('')
const customDockerfile = ref('')
const customInitScript = ref('')

const runtimeOptions = [
  { value: 'docker', label: 'Docker' },
  { value: 'qemu', label: 'QEMU' },
]

const dockerDistroOptions = [
  { value: 'ubuntu:22.04', label: 'ubuntu:22.04' },
  { value: 'ubuntu:24.04', label: 'ubuntu:24.04' },
  { value: 'debian:12', label: 'debian:12' },
  { value: 'fedora:40', label: 'fedora:40' },
  { value: 'alpine:3.19', label: 'alpine:3.19' },
  { value: '__custom__', label: 'Custom...' },
]

const qemuDistroOptions = [
  { value: 'ubuntu:22.04', label: 'ubuntu:22.04' },
  { value: 'ubuntu:24.04', label: 'ubuntu:24.04' },
  { value: '__custom__', label: 'Custom...' },
]

const distroOptions = computed(() =>
  runtimeType.value === 'qemu' ? qemuDistroOptions : dockerDistroOptions,
)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    const src = props.imageDefinition
    activeTab.value = 'definition'
    name.value = src?.name || ''
    description.value = src?.description || ''
    runtimeType.value = (src?.runtime_type as 'docker' | 'qemu') || 'docker'
    const currentBase = src?.base_distro || 'ubuntu:22.04'
    const known = [...dockerDistroOptions, ...qemuDistroOptions].find((opt) => opt.value === currentBase)
    baseDistroPreset.value = known ? currentBase : '__custom__'
    customBaseDistro.value = known ? '' : currentBase
    packages.value = (src?.packages || []).join(', ')
    envVars.value = Object.entries(src?.env_vars || {})
      .map(([k, v]) => `${k}=${v}`)
      .join('\n')
    customDockerfile.value = src?.custom_dockerfile || ''
    customInitScript.value = src?.custom_init_script || ''
  },
)

const title = computed(() => (props.imageDefinition ? 'Edit Image Definition' : 'Create Image Definition'))
const baseDistro = computed(() =>
  baseDistroPreset.value === '__custom__' ? customBaseDistro.value.trim() : baseDistroPreset.value,
)
const isValid = computed(() => !!name.value.trim() && !!baseDistro.value.trim())

function parseEnvVars(raw: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of raw.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const idx = trimmed.indexOf('=')
    if (idx <= 0) continue
    out[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim()
  }
  return out
}

function handleSave(): void {
  if (!isValid.value) return
  emit('saved', {
    name: name.value.trim(),
    description: description.value.trim(),
    runtime_type: runtimeType.value,
    base_distro: baseDistro.value.trim(),
    packages: packages.value
      .split(',')
      .map((v) => v.trim())
      .filter(Boolean),
    env_vars: parseEnvVars(envVars.value),
    custom_dockerfile: customDockerfile.value,
    custom_init_script: customInitScript.value,
  })
}
</script>

<template>
  <Dialog
    :open="open"
    @update:open="(v) => emit('update:open', v)"
  >
    <DialogContent>
      <DialogHeader>
        <DialogTitle>{{ title }}</DialogTitle>
        <DialogDescription>
          Define base distro, packages and advanced build customization.
        </DialogDescription>
      </DialogHeader>

      <form class="space-y-4" @submit.prevent="handleSave">
        <div class="rounded-md border border-border bg-muted/50 p-1">
          <div class="grid grid-cols-2 gap-1">
            <Button type="button" size="sm" :variant="activeTab === 'definition' ? 'secondary' : 'ghost'" @click="activeTab = 'definition'">
              Definition
            </Button>
            <Button type="button" size="sm" :variant="activeTab === 'advanced' ? 'secondary' : 'ghost'" @click="activeTab = 'advanced'">
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

          <div class="grid gap-3 sm:grid-cols-2">
            <div>
              <label class="text-sm font-medium text-foreground mb-1.5 block">Runtime Type</label>
              <Select v-model="runtimeType">
                <SelectTrigger>
                  <SelectValue placeholder="Select runtime" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem
                    v-for="opt in runtimeOptions"
                    :key="opt.value"
                    :value="opt.value"
                  >
                    {{ opt.label }}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <label class="text-sm font-medium text-foreground mb-1.5 block">Base Distro</label>
              <Select v-model="baseDistroPreset">
                <SelectTrigger>
                  <SelectValue placeholder="Select distro" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem
                    v-for="opt in distroOptions"
                    :key="opt.value"
                    :value="opt.value"
                  >
                    {{ opt.label }}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <p v-if="runtimeType === 'qemu'" class="text-xs text-muted-foreground -mt-1">
            QEMU image builds currently support Ubuntu cloud images only.
          </p>

          <div v-if="baseDistroPreset === '__custom__'">
            <label class="text-sm font-medium text-foreground mb-1.5 block">Custom Base Distro</label>
            <Input v-model="customBaseDistro" placeholder="e.g. ubuntu:22.04" />
          </div>

          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Pre-installed Packages</label>
            <Input v-model="packages" placeholder="python3, nodejs, ffmpeg" />
          </div>

          <div>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Baked Environment Variables</label>
            <Textarea v-model="envVars" :rows="4" placeholder="NODE_ENV=production" />
          </div>
        </template>

        <template v-else>
          <p class="text-xs text-muted-foreground">
            The base distro and packages above are applied first. Use this section for advanced customization.
          </p>
          <div v-if="runtimeType === 'docker'">
            <label class="text-sm font-medium text-foreground mb-1.5 block">Custom Dockerfile</label>
            <Textarea v-model="customDockerfile" :rows="8" placeholder="RUN pip install numpy torch" />
          </div>

          <div v-else>
            <label class="text-sm font-medium text-foreground mb-1.5 block">Custom Init Script</label>
            <Textarea v-model="customInitScript" :rows="8" placeholder="#!/bin/bash\napt-get install -y ffmpeg" />
          </div>
        </template>

        <div class="flex justify-end gap-2 pt-2">
          <Button variant="outline" type="button" @click="emit('update:open', false)">Cancel</Button>
          <Button type="submit" :disabled="!isValid">Save</Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
</template>
