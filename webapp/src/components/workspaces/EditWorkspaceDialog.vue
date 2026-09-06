<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Pencil, Check, Key } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import type { Workspace, WorkspaceUpdateIn } from '@/types'
import { RuntimeType } from '@/types'
import { toggleWorkspaceCredentialSelection } from '@/lib/workspaceCredentialSelection'
import { DEFAULT_DESKTOP_HEIGHT, DEFAULT_DESKTOP_WIDTH } from '@/lib/desktopGeometry'
import { useCredentialStore } from '@/stores/credentials'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'

const props = defineProps<{
  workspace: Workspace
  size?: 'default' | 'sm'
  disabled?: boolean
}>()

const credentialStore = useCredentialStore()
const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const router = useRouter()

const open = ref(false)
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const qemuVcpus = ref(2)
const qemuMemoryMb = ref(4096)
const qemuDiskSizeGb = ref(50)
const desktopWidth = ref(1920)
const desktopHeight = ref(1080)
const submitting = ref(false)

const btnSize = computed(() => (props.size === 'sm' ? 'icon-sm' as const : 'icon' as const))
const qemuDefaults = computed(() => {
  const runner = runnerStore.runnerById(props.workspace.runner_id)
  return {
    vcpus: runner?.qemu_default_vcpus ?? 2,
    memoryMb: runner?.qemu_default_memory_mb ?? 4096,
    diskSizeGb: runner?.qemu_default_disk_size_gb ?? 50,
  }
})

const qemuLimits = computed(() => {
  const runner = runnerStore.runnerById(props.workspace.runner_id)
  if (!runner) {
    return {
      minVcpus: 1,
      maxVcpus: 64,
      minMemoryMb: 512,
      maxMemoryMb: 262144,
      minDiskSizeGb: 10,
      maxDiskSizeGb: 2000,
    }
  }
  return {
    minVcpus: runner.qemu_min_vcpus,
    maxVcpus: runner.qemu_max_vcpus,
    minMemoryMb: runner.qemu_min_memory_mb,
    maxMemoryMb: runner.qemu_max_memory_mb,
    minDiskSizeGb: runner.qemu_min_disk_size_gb,
    maxDiskSizeGb: runner.qemu_max_disk_size_gb,
  }
})

function resolveCurrentQemuResources(workspace: Workspace = props.workspace) {
  return {
    vcpus: workspace.qemu_vcpus ?? qemuDefaults.value.vcpus,
    memoryMb: workspace.qemu_memory_mb ?? qemuDefaults.value.memoryMb,
    diskSizeGb: workspace.qemu_disk_size_gb ?? qemuDefaults.value.diskSizeGb,
  }
}

function syncFormWithWorkspace(workspace: Workspace): void {
  const currentQemuResources = resolveCurrentQemuResources(workspace)
  name.value = workspace.name
  selectedCredentialIds.value = [...workspace.credential_ids]
  qemuVcpus.value = currentQemuResources.vcpus
  qemuMemoryMb.value = currentQemuResources.memoryMb
  qemuDiskSizeGb.value = currentQemuResources.diskSizeGb
  desktopWidth.value = workspace.desktop_width || DEFAULT_DESKTOP_WIDTH
  desktopHeight.value = workspace.desktop_height || DEFAULT_DESKTOP_HEIGHT
}

watch(
  () => props.workspace,
  (workspace) => {
    if (open.value) return
    syncFormWithWorkspace(workspace)
  },
  { immediate: true, deep: true },
)

function toggleCredential(id: string): void {
  const credential = credentialStore.credentials.find((entry) => entry.id === id)
  if (!credential) return
  selectedCredentialIds.value = toggleWorkspaceCredentialSelection(
    selectedCredentialIds.value,
    credential,
    credentialStore.credentials,
  )
}

async function handleOpen(): Promise<void> {
  if (props.disabled) return
  open.value = true
  if (!runnerStore.runners.length) {
    await runnerStore.fetchRunners()
  }
  await credentialStore.fetchCredentials()
  syncFormWithWorkspace(props.workspace)
}

async function handleSubmit(): Promise<void> {
  submitting.value = true
  const payload: WorkspaceUpdateIn = {
    name: name.value,
    credential_ids: selectedCredentialIds.value,
  }
  if (props.workspace.runtime_type === RuntimeType.QEMU) {
    const currentQemuResources = resolveCurrentQemuResources()
    if (qemuVcpus.value !== currentQemuResources.vcpus) {
      payload.qemu_vcpus = qemuVcpus.value
    }
    if (qemuMemoryMb.value !== currentQemuResources.memoryMb) {
      payload.qemu_memory_mb = qemuMemoryMb.value
    }
    if (qemuDiskSizeGb.value !== currentQemuResources.diskSizeGb) {
      payload.qemu_disk_size_gb = qemuDiskSizeGb.value
    }
  }
  const currentWidth = props.workspace.desktop_width || DEFAULT_DESKTOP_WIDTH
  const currentHeight = props.workspace.desktop_height || DEFAULT_DESKTOP_HEIGHT
  if (desktopWidth.value !== currentWidth) {
    payload.desktop_width = desktopWidth.value
  }
  if (desktopHeight.value !== currentHeight) {
    payload.desktop_height = desktopHeight.value
  }

  const success = await workspaceStore.updateWorkspace(props.workspace.id, payload)
  submitting.value = false

  if (success) {
    open.value = false
  }
}

function handleClose(): void {
  open.value = false
  syncFormWithWorkspace(props.workspace)
}

async function navigateToCredentials(): Promise<void> {
  handleClose()
  // Settings-Routen sind Redirects aufs Sheet (Schritt 6): Deep-Link nutzen.
  await router.push({ path: '/', query: { settings: 'credentials' } })
}

</script>

<template>
  <Dialog
    :open="open"
    @update:open="(value) => (value ? handleOpen() : handleClose())"
  >
    <DialogTrigger as-child>
      <Button
        variant="ghost"
        :size="btnSize"
        title="Edit workspace"
        :disabled="props.disabled"
        @click.stop="handleOpen"
      >
        <Pencil :size="14" />
      </Button>
    </DialogTrigger>

    <DialogContent>
      <DialogHeader>
        <DialogTitle>Edit Workspace</DialogTitle>
        <DialogDescription>Update the workspace name, desktop size, and attached credentials.</DialogDescription>
      </DialogHeader>

      <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">Name</label>
          <Input v-model="name" :disabled="submitting || props.disabled" placeholder="Workspace name" />
        </div>

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">
            Credentials
            <span class="text-muted-foreground font-normal">(optional)</span>
          </label>

          <div
            v-if="credentialStore.credentials.length"
            class="flex flex-col gap-1.5 max-h-56 overflow-y-auto"
          >
            <button
              v-for="cred in credentialStore.credentials"
              :key="cred.id"
              type="button"
              class="flex items-center gap-2 px-3 py-2 rounded-sm border text-left text-sm transition-colors cursor-pointer"
              :disabled="submitting || props.disabled"
              :class="selectedCredentialIds.includes(cred.id)
                ? 'border-primary bg-primary/5 text-foreground'
                : 'border-border bg-background text-muted-foreground hover:bg-muted'"
              @click="toggleCredential(cred.id)"
            >
              <div
                class="flex items-center justify-center w-4 h-4 rounded-sm border"
                :class="selectedCredentialIds.includes(cred.id)
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'border-border'"
              >
                <Check v-if="selectedCredentialIds.includes(cred.id)" :size="10" />
              </div>
              <span class="flex-1 truncate">{{ cred.name }}</span>
              <span
                v-if="cred.credential_type === 'ssh_key'"
                class="inline-flex items-center gap-1 text-xs text-muted-foreground"
              >
                <Key :size="10" />
                SSH Key
              </span>
              <span v-else-if="cred.target_path" class="text-xs text-muted-foreground">
                {{ cred.target_path }}
              </span>
              <span v-else-if="cred.env_var_name" class="text-xs text-muted-foreground">
                {{ cred.env_var_name }}
              </span>
            </button>
          </div>

          <p v-else class="text-xs text-muted-foreground">
            No credentials available.
            <button type="button" class="underline cursor-pointer" @click="navigateToCredentials">Add credentials</button>
            first.
          </p>
        </div>

        <div class="space-y-3">
          <label class="text-sm font-medium text-foreground block">Desktop size</label>
          <p class="text-xs text-muted-foreground">
            Applies the next time the desktop starts.
          </p>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="text-sm font-medium text-muted-foreground mb-1 block">Width</label>
              <Input
                v-model.number="desktopWidth"
                :disabled="submitting || props.disabled"
                type="number"
                min="800"
                max="3840"
                step="2"
              />
            </div>
            <div>
              <label class="text-sm font-medium text-muted-foreground mb-1 block">Height</label>
              <Input
                v-model.number="desktopHeight"
                :disabled="submitting || props.disabled"
                type="number"
                min="600"
                max="2160"
                step="2"
              />
            </div>
          </div>
        </div>

        <div v-if="workspace.runtime_type === RuntimeType.QEMU" class="space-y-3">
          <label class="text-sm font-medium text-foreground block">QEMU resources</label>

          <div>
            <label class="text-sm font-medium text-muted-foreground mb-1 block">vCPU</label>
            <input v-model.number="qemuVcpus" :disabled="submitting || props.disabled" type="range" class="w-full accent-primary" :min="qemuLimits.minVcpus" :max="qemuLimits.maxVcpus" step="1" />
            <input v-model.number="qemuVcpus" :disabled="submitting || props.disabled" type="number" class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="qemuLimits.minVcpus" :max="qemuLimits.maxVcpus" step="1" />
          </div>

          <div>
            <label class="text-sm font-medium text-muted-foreground mb-1 block">RAM (MiB)</label>
            <input v-model.number="qemuMemoryMb" :disabled="submitting || props.disabled" type="range" class="w-full accent-primary" :min="qemuLimits.minMemoryMb" :max="qemuLimits.maxMemoryMb" step="256" />
            <input v-model.number="qemuMemoryMb" :disabled="submitting || props.disabled" type="number" class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="qemuLimits.minMemoryMb" :max="qemuLimits.maxMemoryMb" step="256" />
          </div>

          <div>
            <label class="text-sm font-medium text-muted-foreground mb-1 block">Storage (GiB)</label>
            <input v-model.number="qemuDiskSizeGb" :disabled="submitting || props.disabled" type="range" class="w-full accent-primary" :min="qemuLimits.minDiskSizeGb" :max="qemuLimits.maxDiskSizeGb" step="1" />
            <input v-model.number="qemuDiskSizeGb" :disabled="submitting || props.disabled" type="number" class="mt-1 w-full rounded border border-border bg-background px-1.5 py-0.5 text-xs font-mono text-foreground focus:outline-none focus:border-primary" :min="qemuLimits.minDiskSizeGb" :max="qemuLimits.maxDiskSizeGb" step="1" />
          </div>
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <Button variant="outline" type="button" :disabled="submitting" @click="handleClose">Cancel</Button>
          <Button type="submit" :disabled="submitting || props.disabled || !name.trim()">
            {{ submitting ? 'Saving…' : 'Save Changes' }}
          </Button>
        </div>
      </form>
    </DialogContent>
  </Dialog>
</template>
