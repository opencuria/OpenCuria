<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { Pencil, Check, Key } from 'lucide-vue-next'

import { UiButton, UiDialog, UiInput } from '@/components/ui'
import type { Workspace } from '@/types'
import { RuntimeType } from '@/types'
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

const open = ref(false)
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const qemuVcpus = ref(2)
const qemuMemoryMb = ref(4096)
const qemuDiskSizeGb = ref(50)
const submitting = ref(false)

const btnSize = computed(() => (props.size === 'sm' ? 'icon-sm' as const : 'icon' as const))

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

watch(
  () => props.workspace,
  (workspace) => {
    if (open.value) return
    name.value = workspace.name
    selectedCredentialIds.value = [...workspace.credential_ids]
    qemuVcpus.value = workspace.qemu_vcpus ?? 2
    qemuMemoryMb.value = workspace.qemu_memory_mb ?? 4096
    qemuDiskSizeGb.value = workspace.qemu_disk_size_gb ?? 50
  },
  { immediate: true, deep: true },
)

function toggleCredential(id: string): void {
  const index = selectedCredentialIds.value.indexOf(id)
  if (index === -1) {
    selectedCredentialIds.value.push(id)
  } else {
    selectedCredentialIds.value.splice(index, 1)
  }
}

async function handleOpen(): Promise<void> {
  if (props.disabled) return
  open.value = true
  if (!runnerStore.runners.length) {
    await runnerStore.fetchRunners()
  }
  await credentialStore.fetchCredentials()
  name.value = props.workspace.name
  selectedCredentialIds.value = [...props.workspace.credential_ids]
  qemuVcpus.value = props.workspace.qemu_vcpus ?? 2
  qemuMemoryMb.value = props.workspace.qemu_memory_mb ?? 4096
  qemuDiskSizeGb.value = props.workspace.qemu_disk_size_gb ?? 50
}

async function handleSubmit(): Promise<void> {
  submitting.value = true
  const success = await workspaceStore.updateWorkspace(props.workspace.id, {
    name: name.value,
    credential_ids: selectedCredentialIds.value,
    ...(props.workspace.runtime_type === RuntimeType.QEMU
      ? {
          qemu_vcpus: qemuVcpus.value,
          qemu_memory_mb: qemuMemoryMb.value,
          qemu_disk_size_gb: qemuDiskSizeGb.value,
        }
      : {}),
  })
  submitting.value = false

  if (success) {
    open.value = false
  }
}

function handleClose(): void {
  open.value = false
  name.value = props.workspace.name
  selectedCredentialIds.value = [...props.workspace.credential_ids]
  qemuVcpus.value = props.workspace.qemu_vcpus ?? 2
  qemuMemoryMb.value = props.workspace.qemu_memory_mb ?? 4096
  qemuDiskSizeGb.value = props.workspace.qemu_disk_size_gb ?? 50
}
</script>

<template>
  <UiDialog
    :open="open"
    title="Edit Workspace"
    description="Update the workspace name and attached credentials."
    @update:open="(value) => (value ? handleOpen() : handleClose())"
  >
    <template #trigger>
      <UiButton
        variant="ghost"
        :size="btnSize"
        title="Edit workspace"
        :disabled="props.disabled"
        @click.stop="handleOpen"
      >
        <Pencil :size="14" />
      </UiButton>
    </template>

    <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Name</label>
        <UiInput v-model="name" :disabled="submitting || props.disabled" placeholder="Workspace name" />
      </div>

      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">
          Credentials
          <span class="text-muted-fg font-normal">(optional)</span>
        </label>

        <div
          v-if="credentialStore.credentials.length"
          class="flex flex-col gap-1.5 max-h-56 overflow-y-auto"
        >
          <button
            v-for="cred in credentialStore.credentials"
            :key="cred.id"
            type="button"
            class="flex items-center gap-2 px-3 py-2 rounded-[var(--radius-sm)] border text-left text-sm transition-colors cursor-pointer"
            :disabled="submitting || props.disabled"
            :class="selectedCredentialIds.includes(cred.id)
              ? 'border-primary bg-primary/5 text-fg'
              : 'border-border bg-bg text-muted-fg hover:bg-surface-hover'"
            @click="toggleCredential(cred.id)"
          >
            <div
              class="flex items-center justify-center w-4 h-4 rounded-sm border"
              :class="selectedCredentialIds.includes(cred.id)
                ? 'border-primary bg-primary text-primary-fg'
                : 'border-border'"
            >
              <Check v-if="selectedCredentialIds.includes(cred.id)" :size="10" />
            </div>
            <span class="flex-1 truncate">{{ cred.name }}</span>
            <span
              v-if="cred.credential_type === 'ssh_key'"
              class="inline-flex items-center gap-1 text-xs text-muted-fg"
            >
              <Key :size="10" />
              SSH Key
            </span>
            <span v-else-if="cred.env_var_name" class="text-xs text-muted-fg">
              {{ cred.env_var_name }}
            </span>
          </button>
        </div>

        <p v-else class="text-xs text-muted-fg">
          No credentials available.
          <RouterLink to="/credentials" class="underline">Add credentials</RouterLink>
          first.
        </p>
      </div>

      <div v-if="workspace.runtime_type === RuntimeType.QEMU" class="space-y-3">
        <label class="text-sm font-medium text-fg block">QEMU resources</label>

        <div>
          <label class="text-sm font-medium text-muted-fg mb-1 block">vCPU</label>
          <input v-model.number="qemuVcpus" :disabled="submitting || props.disabled" type="range" class="w-full accent-primary" :min="qemuLimits.minVcpus" :max="qemuLimits.maxVcpus" step="1" />
          <input v-model.number="qemuVcpus" :disabled="submitting || props.disabled" type="number" class="mt-1 w-full rounded border border-border bg-bg px-1.5 py-0.5 text-xs font-mono text-fg focus:outline-none focus:border-primary" :min="qemuLimits.minVcpus" :max="qemuLimits.maxVcpus" step="1" />
        </div>

        <div>
          <label class="text-sm font-medium text-muted-fg mb-1 block">RAM (MiB)</label>
          <input v-model.number="qemuMemoryMb" :disabled="submitting || props.disabled" type="range" class="w-full accent-primary" :min="qemuLimits.minMemoryMb" :max="qemuLimits.maxMemoryMb" step="256" />
          <input v-model.number="qemuMemoryMb" :disabled="submitting || props.disabled" type="number" class="mt-1 w-full rounded border border-border bg-bg px-1.5 py-0.5 text-xs font-mono text-fg focus:outline-none focus:border-primary" :min="qemuLimits.minMemoryMb" :max="qemuLimits.maxMemoryMb" step="256" />
        </div>

        <div>
          <label class="text-sm font-medium text-muted-fg mb-1 block">Storage (GiB)</label>
          <input v-model.number="qemuDiskSizeGb" :disabled="submitting || props.disabled" type="range" class="w-full accent-primary" :min="qemuLimits.minDiskSizeGb" :max="qemuLimits.maxDiskSizeGb" step="1" />
          <input v-model.number="qemuDiskSizeGb" :disabled="submitting || props.disabled" type="number" class="mt-1 w-full rounded border border-border bg-bg px-1.5 py-0.5 text-xs font-mono text-fg focus:outline-none focus:border-primary" :min="qemuLimits.minDiskSizeGb" :max="qemuLimits.maxDiskSizeGb" step="1" />
        </div>
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" :disabled="submitting" @click="handleClose">Cancel</UiButton>
        <UiButton type="submit" :disabled="submitting || props.disabled || !name.trim()">
          {{ submitting ? 'Saving…' : 'Save Changes' }}
        </UiButton>
      </div>
    </form>
  </UiDialog>
</template>
