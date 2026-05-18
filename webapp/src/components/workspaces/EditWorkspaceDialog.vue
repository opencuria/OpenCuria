<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Pencil, Check, Key } from 'lucide-vue-next'

import { UiButton, UiDialog, UiInput } from '@/components/ui'
import type {
  Workspace,
  WorkspaceDesktopStartCommand,
  WorkspaceUpdateIn,
} from '@/types'
import { RuntimeType } from '@/types'
import { toggleWorkspaceCredentialSelection } from '@/lib/workspaceCredentialSelection'
import { useNotificationStore } from '@/stores/notifications'
import { useCredentialStore } from '@/stores/credentials'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import * as workspacesApi from '@/services/workspaces.api'

const props = defineProps<{
  workspace: Workspace
  size?: 'default' | 'sm'
  disabled?: boolean
}>()

const emit = defineEmits<{
  desktopStartCommandsUpdated: []
}>()

const credentialStore = useCredentialStore()
const notificationStore = useNotificationStore()
const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const router = useRouter()

const open = ref(false)
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const qemuVcpus = ref(2)
const qemuMemoryMb = ref(4096)
const qemuDiskSizeGb = ref(50)
const submitting = ref(false)
const desktopStartCommands = ref<Array<WorkspaceDesktopStartCommand & { localId: string }>>([])
const initialDesktopStartCommands = ref<WorkspaceDesktopStartCommand[]>([])

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
}

function cloneDesktopStartCommands(
  commands: WorkspaceDesktopStartCommand[],
): Array<WorkspaceDesktopStartCommand & { localId: string }> {
  return commands.map((command) => ({
    ...command,
    localId: command.id,
  }))
}

async function loadDesktopStartCommands(): Promise<void> {
  const commands = await workspacesApi.listDesktopStartCommands(props.workspace.id)
  initialDesktopStartCommands.value = commands
  desktopStartCommands.value = cloneDesktopStartCommands(commands)
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
  await loadDesktopStartCommands()
  syncFormWithWorkspace(props.workspace)
}

function addDesktopStartCommand(): void {
  desktopStartCommands.value.push({
    id: '',
    workspace_id: props.workspace.id,
    localId: `new-${Date.now()}-${desktopStartCommands.value.length}`,
    name: '',
    command: '',
    created_at: '',
    updated_at: '',
  })
}

function removeDesktopStartCommand(localId: string): void {
  desktopStartCommands.value = desktopStartCommands.value.filter(
    (command) => command.localId !== localId,
  )
}

const hasInvalidDesktopStartCommands = computed(() =>
  desktopStartCommands.value.some(
    (command) => !command.name.trim() || !command.command.trim(),
  ),
)

async function syncDesktopStartCommands(): Promise<boolean> {
  const initialById = new Map(initialDesktopStartCommands.value.map((command) => [command.id, command]))
  const currentIds = new Set(
    desktopStartCommands.value.filter((command) => command.id).map((command) => command.id),
  )

  for (const command of initialDesktopStartCommands.value) {
    if (!currentIds.has(command.id)) {
      await workspacesApi.deleteDesktopStartCommand(props.workspace.id, command.id)
    }
  }

  for (const command of desktopStartCommands.value) {
    const payload = {
      name: command.name.trim(),
      command: command.command.trim(),
    }
    if (!command.id) {
      await workspacesApi.createDesktopStartCommand(props.workspace.id, payload)
      continue
    }

    const initial = initialById.get(command.id)
    if (!initial) continue

    const changedFields: { name?: string; command?: string } = {}
    if (initial.name !== payload.name) changedFields.name = payload.name
    if (initial.command !== payload.command) changedFields.command = payload.command

    if (Object.keys(changedFields).length > 0) {
      await workspacesApi.updateDesktopStartCommand(
        props.workspace.id,
        command.id,
        changedFields,
      )
    }
  }

  const desktopStartCommandsChanged =
    desktopStartCommands.value.length !== initialDesktopStartCommands.value.length
    || desktopStartCommands.value.some((command, index) => {
      const initial = initialDesktopStartCommands.value[index]
      if (!initial) return true
      return command.id !== initial.id
        || command.name.trim() !== initial.name
        || command.command.trim() !== initial.command
    })

  if (desktopStartCommandsChanged) {
    emit('desktopStartCommandsUpdated')
  }
  return desktopStartCommandsChanged
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

  const success = await workspaceStore.updateWorkspace(props.workspace.id, payload)
  if (!success) {
    submitting.value = false
    return
  }

  try {
    await syncDesktopStartCommands()
    open.value = false
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : 'Failed to update desktop start commands'
    notificationStore.error('Update failed', message)
    await loadDesktopStartCommands()
  } finally {
    submitting.value = false
  }
}

function handleClose(): void {
  open.value = false
  syncFormWithWorkspace(props.workspace)
  desktopStartCommands.value = cloneDesktopStartCommands(initialDesktopStartCommands.value)
}

async function navigateToCredentials(): Promise<void> {
  handleClose()
  await router.push({ name: 'credentials' })
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
            <span v-else-if="cred.target_path" class="text-xs text-muted-fg">
              {{ cred.target_path }}
            </span>
            <span v-else-if="cred.env_var_name" class="text-xs text-muted-fg">
              {{ cred.env_var_name }}
            </span>
          </button>
        </div>

        <p v-else class="text-xs text-muted-fg">
          No credentials available.
          <button type="button" class="underline cursor-pointer" @click="navigateToCredentials">Add credentials</button>
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

      <div class="space-y-3">
        <div class="flex items-center justify-between gap-2">
          <div>
            <label class="text-sm font-medium text-fg block">Desktop start commands</label>
            <p class="text-xs text-muted-fg">
              Configure the commands available from the desktop launcher.
            </p>
          </div>
          <UiButton
            variant="outline"
            size="sm"
            type="button"
            :disabled="submitting || props.disabled"
            @click="addDesktopStartCommand"
          >
            Add command
          </UiButton>
        </div>

        <div v-if="desktopStartCommands.length" class="space-y-3">
          <div
            v-for="desktopStartCommand in desktopStartCommands"
            :key="desktopStartCommand.localId"
            class="rounded-[var(--radius-md)] border border-border bg-bg p-3 space-y-3"
          >
            <div class="grid gap-3 sm:grid-cols-[minmax(0,160px)_minmax(0,1fr)_auto] sm:items-end">
              <div>
                <label class="mb-1 block text-xs font-medium text-muted-fg">Name</label>
                <UiInput
                  v-model="desktopStartCommand.name"
                  :disabled="submitting || props.disabled"
                  placeholder="Browser"
                />
              </div>
              <div>
                <label class="mb-1 block text-xs font-medium text-muted-fg">Command</label>
                <UiInput
                  v-model="desktopStartCommand.command"
                  :disabled="submitting || props.disabled"
                  placeholder="/usr/local/bin/opencuria-desktop-browser"
                />
              </div>
              <UiButton
                variant="ghost"
                type="button"
                class="text-error hover:text-error"
                :disabled="submitting || props.disabled"
                @click="removeDesktopStartCommand(desktopStartCommand.localId)"
              >
                Delete
              </UiButton>
            </div>
          </div>
        </div>

        <p v-else class="text-xs text-muted-fg">
          No custom desktop start commands configured. Starting the desktop falls back to the browser default.
        </p>
      </div>

      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" :disabled="submitting" @click="handleClose">Cancel</UiButton>
        <UiButton
          type="submit"
          :disabled="submitting || props.disabled || !name.trim() || hasInvalidDesktopStartCommands"
        >
          {{ submitting ? 'Saving…' : 'Save Changes' }}
        </UiButton>
      </div>
    </form>
  </UiDialog>
</template>
