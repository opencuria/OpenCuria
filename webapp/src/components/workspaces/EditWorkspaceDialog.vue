<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Pencil, Check, Key, Puzzle } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import type { Credential, Workspace, WorkspacePlugin, WorkspaceUpdateIn } from '@/types'
import { RuntimeType } from '@/types'
import { toggleWorkspaceCredentialSelection } from '@/lib/workspaceCredentialSelection'
import { DEFAULT_DESKTOP_HEIGHT, DEFAULT_DESKTOP_WIDTH } from '@/lib/desktopGeometry'
import { useCredentialStore } from '@/stores/credentials'
import { useNotificationStore } from '@/stores/notifications'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { usePluginStore } from '@/stores/plugins'

const props = defineProps<{
  workspace: Workspace
  size?: 'default' | 'sm'
  disabled?: boolean
}>()

const credentialStore = useCredentialStore()
const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const pluginStore = usePluginStore()
const notifications = useNotificationStore()
const router = useRouter()

const open = ref(false)
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const selectedPluginIds = ref<string[]>([])
const initialPluginIds = ref<string[]>([])
const qemuVcpus = ref(2)
const qemuMemoryMb = ref(4096)
const qemuDiskSizeGb = ref(50)
const desktopWidth = ref(1920)
const desktopHeight = ref(1080)
const submitting = ref(false)

const btnSize = computed(() => (props.size === 'sm' ? 'icon-sm' as const : 'icon' as const))

const workspacePluginList = computed<WorkspacePlugin[]>(
  () => pluginStore.workspacePlugins[props.workspace.id] ?? [],
)
const workspacePluginsLoading = computed(
  () => pluginStore.workspacePluginsLoading[props.workspace.id] ?? false,
)
const workspacePluginsError = computed(
  () => pluginStore.workspacePluginsError[props.workspace.id] ?? null,
)

/** Service ids covered by the locally selected credentials. */
const attachedServiceIds = computed(() => {
  const byId = new Map(credentialStore.credentials.map((c) => [c.id, c]))
  const ids = new Set<string>()
  for (const id of selectedCredentialIds.value) {
    const cred = byId.get(id)
    if (cred) ids.add(cred.service_id)
  }
  return ids
})

interface PluginGap {
  key: string
  service_id: string
  service_slug: string
}

/**
 * Missing required credentials for a plugin, recomputed from the local
 * credential selection. Prefers the full catalog requirements (so newly
 * detached services are detected); falls back to the server-reported gaps.
 */
function missingForPlugin(pluginId: string): PluginGap[] {
  const catalog = pluginStore.plugins.find((p) => p.id === pluginId)
  if (catalog) {
    return catalog.credential_requirements
      .filter((req) => req.required && !attachedServiceIds.value.has(req.service_id))
      .map((req) => ({
        key: req.key,
        service_id: req.service_id,
        service_slug: req.service_slug,
      }))
  }
  const entry = workspacePluginList.value.find((p) => p.id === pluginId)
  if (!entry) return []
  return (entry.missing_required_credentials ?? []).filter(
    (gap) => !attachedServiceIds.value.has(gap.service_id),
  )
}

function credentialsForService(serviceId: string): Credential[] {
  return credentialStore.credentials.filter((c) => c.service_id === serviceId)
}

const blockingPluginIds = computed(() =>
  selectedPluginIds.value.filter((id) => missingForPlugin(id).length > 0),
)

const pluginChanged = computed(() => {
  const initial = new Set(initialPluginIds.value)
  const selected = new Set(selectedPluginIds.value)
  if (initial.size !== selected.size) return true
  for (const id of selected) {
    if (!initial.has(id)) return true
  }
  return false
})
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

function mergeWorkspaceWithPayload(
  workspace: Workspace,
  payload: WorkspaceUpdateIn,
  credentialIds: string[],
): Workspace {
  return {
    ...workspace,
    name: payload.name ?? workspace.name,
    credential_ids: [...credentialIds],
    qemu_vcpus: payload.qemu_vcpus ?? workspace.qemu_vcpus,
    qemu_memory_mb: payload.qemu_memory_mb ?? workspace.qemu_memory_mb,
    qemu_disk_size_gb: payload.qemu_disk_size_gb ?? workspace.qemu_disk_size_gb,
    desktop_width: payload.desktop_width ?? workspace.desktop_width,
    desktop_height: payload.desktop_height ?? workspace.desktop_height,
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

function syncPluginsFromStore(): void {
  const list = pluginStore.workspacePlugins[props.workspace.id] ?? []
  const enabled = list.filter((p) => p.workspace_enabled).map((p) => p.id)
  initialPluginIds.value = [...enabled]
  selectedPluginIds.value = [...enabled]
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

function togglePlugin(id: string): void {
  if (selectedPluginIds.value.includes(id)) {
    selectedPluginIds.value = selectedPluginIds.value.filter((entry) => entry !== id)
  } else {
    selectedPluginIds.value = [...selectedPluginIds.value, id]
  }
}

function attachCredential(id: string): void {
  // Idempotent attach: never detach an already-selected credential from
  // an "Attach" button (toggling here would surprise users).
  if (selectedCredentialIds.value.includes(id)) return
  toggleCredential(id)
}

/** Plugin toggles are only safe when the list loaded (backend guards removal otherwise). */
const pluginsUnavailable = computed(() => workspacePluginsError.value !== null)

function buildWorkspacePayload(): WorkspaceUpdateIn {
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
  return payload
}

async function handleOpen(): Promise<void> {
  if (props.disabled) return
  open.value = true
  if (!runnerStore.runners.length) {
    await runnerStore.fetchRunners()
  }
  await Promise.all([
    credentialStore.fetchCredentials(),
    pluginStore.fetchWorkspacePlugins(props.workspace.id),
    // Catalog requirements allow local missing-recomputation on
    // credential (de)selection; best-effort when already loaded.
    pluginStore.plugins.length ? Promise.resolve() : pluginStore.fetchPlugins(),
  ])
  syncFormWithWorkspace(props.workspace)
  syncPluginsFromStore()
}

async function handleSubmit(): Promise<void> {
  // Block saving while a selected plugin misses required credentials —
  // the user can attach matching credentials in this same form.
  if (blockingPluginIds.value.length > 0) return
  submitting.value = true
  try {
    const payload = buildWorkspacePayload()

    if (!pluginChanged.value) {
      const success = await workspaceStore.updateWorkspace(props.workspace.id, payload)
      if (success) {
        // Local form now matches the saved server state; resyncing the
        // props-backed form avoids stale cancel-restore after edits.
        syncFormWithWorkspace(mergeWorkspaceWithPayload(props.workspace, payload, [...selectedCredentialIds.value]))
        open.value = false
      }
      return
    }

    const initial = [...initialPluginIds.value]
    const selected = [...selectedPluginIds.value]
    const removed = initial.filter((id) => !selected.includes(id))

    // (a) Deactivate removed plugins first so credentials can be removed after.
    if (removed.length > 0) {
      const subset = initial.filter((id) => selected.includes(id))
      const deactivated = await pluginStore.setWorkspacePlugins(
        props.workspace.id,
        subset,
        { notify: false },
      )
      if (!deactivated) {
        await pluginStore.resyncWorkspacePlugins(props.workspace.id)
        syncPluginsFromStore()
        return
      }
    }

    // (b) Patch workspace (name/credentials/resources) without toasting:
    // the final plugin step may still fail, and a success toast here
    // would misreport a partial save. Toasts are emitted below instead.
    const wsSuccess = await workspaceStore.updateWorkspace(props.workspace.id, payload, { notify: false })
    if (!wsSuccess) {
      await pluginStore.resyncWorkspacePlugins(props.workspace.id)
      syncPluginsFromStore()
      // Reflect the unchanged server state so a later Cancel cannot
      // restore stale local values.
      syncFormWithWorkspace(props.workspace)
      return
    }

    // (c) Activate the final plugin set after credentials are attached.
    const final = await pluginStore.setWorkspacePlugins(props.workspace.id, selected)
    if (!final) {
      // Workspace PATCH already applied: stay open, resync the plugin
      // list, and surface a partial-save warning (not a success).
      await workspaceStore.fetchWorkspaceDetail(props.workspace.id).catch(() => {})
      await pluginStore.resyncWorkspacePlugins(props.workspace.id)
      syncFormWithWorkspace(props.workspace)
      syncPluginsFromStore()
      notifications.warning(
        'Partially saved',
        'Workspace settings were saved, but plugin activation failed. Review the plugin state before retrying.',
      )
      return
    }

    initialPluginIds.value = [...selected]
    notifications.success('Workspace updated', 'The workspace settings were saved.')
    // Keep local form in sync with the committed server state.
    syncFormWithWorkspace(mergeWorkspaceWithPayload(props.workspace, payload, [...selectedCredentialIds.value]))
    open.value = false
  } finally {
    submitting.value = false
  }
}

function handleClose(): void {
  open.value = false
  syncFormWithWorkspace(props.workspace)
  syncPluginsFromStore()
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

    <DialogContent class="sm:max-w-xl">
      <DialogHeader>
        <DialogTitle>Edit Workspace</DialogTitle>
        <DialogDescription>Update the workspace name, desktop size, credentials, and plugins.</DialogDescription>
      </DialogHeader>

      <DialogBody>
      <form id="edit-workspace-form" class="flex flex-col gap-4" @submit.prevent="handleSubmit">
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

        <div>
          <label class="text-sm font-medium text-foreground mb-1.5 block">
            Plugins
            <span class="text-muted-foreground font-normal">(optional)</span>
          </label>

          <div
            v-if="workspacePluginsLoading"
            class="flex items-center gap-2 py-2 text-xs text-muted-foreground"
            data-testid="workspace-plugins-loading"
          >
            <span>Loading plugins…</span>
          </div>
          <p
            v-else-if="workspacePluginsError"
            class="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive"
            data-testid="workspace-plugins-error"
          >
            {{ workspacePluginsError }} Plugin changes are disabled until plugins load; workspace settings can still be saved.
          </p>
          <p
            v-else-if="!workspacePluginList.length"
            class="text-xs text-muted-foreground"
            data-testid="workspace-plugins-empty"
          >
            No plugins enabled for this organization.
          </p>
          <div v-else class="flex flex-col gap-1.5 max-h-64 overflow-y-auto">
            <div
              v-for="plugin in workspacePluginList"
              :key="plugin.id"
              class="rounded-sm border px-3 py-2"
              :class="selectedPluginIds.includes(plugin.id)
                ? 'border-primary bg-primary/5'
                : 'border-border bg-background'"
              :data-testid="`workspace-plugin-${plugin.id}`"
            >
              <button
                type="button"
                class="flex w-full items-center gap-2 text-left text-sm transition-colors cursor-pointer"
                :disabled="submitting || props.disabled || pluginsUnavailable"
                :aria-pressed="selectedPluginIds.includes(plugin.id)"
                @click="togglePlugin(plugin.id)"
              >
                <div
                  class="flex items-center justify-center w-4 h-4 rounded-sm border"
                  :class="selectedPluginIds.includes(plugin.id)
                    ? 'border-primary bg-primary text-primary-foreground'
                    : 'border-border'"
                >
                  <Check v-if="selectedPluginIds.includes(plugin.id)" :size="10" />
                </div>
                <Puzzle :size="14" class="shrink-0 text-muted-foreground" />
                <span class="flex-1 truncate text-foreground">{{ plugin.name }}</span>
                <span
                  v-if="selectedPluginIds.includes(plugin.id) && missingForPlugin(plugin.id).length"
                  class="text-xs font-medium text-destructive"
                  :data-testid="`workspace-plugin-status-${plugin.id}`"
                >
                  Setup needed
                </span>
                <span
                  v-else-if="missingForPlugin(plugin.id).length"
                  class="text-xs text-muted-foreground"
                  :data-testid="`workspace-plugin-status-${plugin.id}`"
                >
                  Setup needed
                </span>
                <span
                  v-else
                  class="text-xs text-muted-foreground"
                  :data-testid="`workspace-plugin-status-${plugin.id}`"
                >
                  Ready
                </span>
              </button>
              <p v-if="plugin.description" class="mt-1 text-xs text-muted-foreground line-clamp-2">
                {{ plugin.description }}
              </p>
              <div
                v-if="selectedPluginIds.includes(plugin.id) && missingForPlugin(plugin.id).length"
                class="mt-2 space-y-1.5 rounded-sm border border-destructive/30 bg-destructive/5 p-2"
                :data-testid="`workspace-plugin-missing-${plugin.id}`"
              >
                <p class="text-xs text-destructive">Missing required credentials:</p>
                <div
                  v-for="gap in missingForPlugin(plugin.id)"
                  :key="gap.service_id"
                  class="space-y-1"
                >
                  <p class="font-mono text-xs text-muted-foreground">
                    {{ gap.service_slug }} ({{ gap.key }})
                  </p>
                  <div v-if="credentialsForService(gap.service_id).length" class="flex flex-wrap gap-1.5">
                    <Button
                      v-for="cred in credentialsForService(gap.service_id)"
                      :key="cred.id"
                      size="sm"
                      variant="outline"
                      type="button"
                      :data-testid="`workspace-plugin-attach-${plugin.id}-${cred.id}`"
                      :disabled="submitting || props.disabled || selectedCredentialIds.includes(cred.id)"
                      @click="attachCredential(cred.id)"
                    >
                      {{ selectedCredentialIds.includes(cred.id) ? `Attached ${cred.name}` : `Attach ${cred.name}` }}
                    </Button>
                  </div>
                  <p v-else class="text-xs text-muted-foreground">
                    No matching credential.
                    <button type="button" class="underline cursor-pointer" @click="navigateToCredentials">
                      Manage credentials
                    </button>
                  </p>
                </div>
              </div>
            </div>
          </div>
          <p
            v-if="blockingPluginIds.length"
            class="mt-1.5 text-xs text-destructive"
            data-testid="workspace-plugins-blocked"
          >
            Attach the required credentials above before saving.
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

      </form>
      </DialogBody>

      <DialogFooter>
        <Button variant="outline" type="button" :disabled="submitting" @click="handleClose">Cancel</Button>
        <Button
          type="submit"
          form="edit-workspace-form"
          data-testid="edit-workspace-save"
          :disabled="submitting || props.disabled || !name.trim() || blockingPluginIds.length > 0 || (pluginChanged && pluginsUnavailable)"
        >
          {{ submitting ? 'Saving…' : 'Save Changes' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
