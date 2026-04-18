<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { useRouter } from 'vue-router'
import { UiDialog, UiInput, UiButton, UiSelect } from '@/components/ui'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { useCredentialStore } from '@/stores/credentials'
import { useImageStore } from '@/stores/images'
import { RuntimeType } from '@/types'
import { filterRunnersByRuntime, runnerSupportsRuntime } from '@/lib/runtimeSupport'
import { toggleWorkspaceCredentialSelection } from '@/lib/workspaceCredentialSelection'
import { X, Check, Key, Camera } from 'lucide-vue-next'
import type { ImageArtifact, RunnerImageBuild } from '@/types'

type SelectableImageKind = 'definition' | 'captured'

interface SelectableImageOption {
  value: string
  kind: SelectableImageKind
  label: string
  runtimeType: RuntimeType
  imageArtifact?: ImageArtifact
  runnerBuilds?: RunnerImageBuild[]
}

const workspaceStore = useWorkspaceStore()
const runnerStore = useRunnerStore()
const credentialStore = useCredentialStore()
const imageStore = useImageStore()
const router = useRouter()

const open = ref(false)
const activeTab = ref<'basic' | 'advanced'>('basic')
const name = ref('')
const selectedCredentialIds = ref<string[]>([])
const runnerId = ref('')
const runtimeType = ref<string>(RuntimeType.QEMU)
const qemuVcpus = ref(2)
const qemuMemoryMb = ref(4096)
const qemuDiskSizeGb = ref(50)
const repoInput = ref('')
const repos = ref<string[]>([])
const submitting = ref(false)

// Image-based creation
const selectedImageValue = ref('')

const imageOptions = computed(() => {
  const definitionOptions: SelectableImageOption[] = []
  for (const definition of imageStore.imageDefinitions) {
    if (!definition.is_active) continue
    const activeBuilds = (imageStore.runnerBuildsByDefinition[definition.id] || [])
      .filter((build) => {
        if (build.status !== 'active' || !build.image_artifact_id) return false
        return runnerStore.onlineRunners.some(
          (runner) =>
            runner.id === build.runner_id &&
            runnerSupportsRuntime(runner, definition.runtime_type),
        )
      })

    if (!activeBuilds.length) continue

    definitionOptions.push({
      value: `definition:${definition.id}`,
      kind: 'definition',
      label: `● ${definition.name} [${definition.runtime_type}]`,
      runtimeType: definition.runtime_type === 'docker' ? RuntimeType.DOCKER : RuntimeType.QEMU,
      runnerBuilds: activeBuilds,
    })
  }

  const capturedOptions: SelectableImageOption[] = imageStore.images
    .filter(
      (artifact) => {
        const sourceRunner = artifact.source_runner_id
          ? runnerStore.runners.find((runner) => runner.id === artifact.source_runner_id)
          : null
        return (
        artifact.artifact_kind === 'captured' &&
        artifact.status === 'ready' &&
        artifact.source_runner_online === true &&
        runnerSupportsRuntime(sourceRunner, artifact.runtime_type || RuntimeType.QEMU)
        )
      },
    )
    .map((artifact) => ({
      value: `captured:${artifact.id}`,
      kind: 'captured',
      label: `○ ${artifact.name} [${(artifact.runtime_type || 'qemu').toString()}]`,
      runtimeType: artifact.runtime_type === RuntimeType.DOCKER ? RuntimeType.DOCKER : RuntimeType.QEMU,
      imageArtifact: artifact,
    }))

  const options = [
    ...(definitionOptions.length
      ? [{ value: '__group_definitions__', label: '── Organization Images ──────────────' }]
      : []),
    ...definitionOptions,
    ...(capturedOptions.length
      ? [{ value: '__group_captured__', label: '── Captured Images ──────────────────' }]
      : []),
    ...capturedOptions,
  ]

  return options
})

const selectableOptionsByValue = computed<Record<string, SelectableImageOption>>(() => {
  const map: Record<string, SelectableImageOption> = {}
  for (const option of imageOptions.value) {
    if (option.value.startsWith('__group_')) continue
    map[option.value] = option as SelectableImageOption
  }
  return map
})

const selectedImageOption = computed<SelectableImageOption | null>(() => {
  if (!selectedImageValue.value) return null
  return selectableOptionsByValue.value[selectedImageValue.value] ?? null
})

const isFromImage = computed(() => !!selectedImageOption.value)
const isCapturedClone = computed(() => selectedImageOption.value?.kind === 'captured')

const compatibleRunnerOptions = computed(() => {
  const option = selectedImageOption.value
  if (!option) return []

  if (option.kind === 'definition') {
    return (option.runnerBuilds || [])
      .map((build) => {
        const runner = runnerStore.onlineRunners.find((entry) => entry.id === build.runner_id)
        if (!runner) return null
        return {
          value: runner.id,
          label: runner.name || runner.id.slice(0, 8),
        }
      })
      .filter((entry): entry is { value: string; label: string } => entry !== null)
  }

  if (option.imageArtifact?.source_runner_id) {
    const runner = runnerStore.runners.find(
      (entry) => entry.id === option.imageArtifact?.source_runner_id,
    )
    if (
      !runner ||
      runner.status !== 'online' ||
      !runnerSupportsRuntime(runner, option.runtimeType)
    ) return []
    return [{ value: runner.id, label: runner.name || runner.id.slice(0, 8) }]
  }

  return []
})

const selectedRunnerBuild = computed(() => {
  const option = selectedImageOption.value
  if (!option || option.kind !== 'definition') return null
  const selectedRunnerId = runnerId.value || compatibleRunnerOptions.value[0]?.value
  if (!selectedRunnerId) return null
  return (option.runnerBuilds || []).find((build) => build.runner_id === selectedRunnerId) ?? null
})

const selectedDefinitionArtifactId = computed<string | null>(() =>
  selectedRunnerBuild.value?.image_artifact_id || null,
)

watch(selectedImageOption, (option, previousOption) => {
  if (option) {
    runtimeType.value = option.runtimeType
  }
  if (previousOption) {
    selectedCredentialIds.value = []
  }
})

watch(compatibleRunnerOptions, (runnerOptions) => {
  const compatibleRunnerIds = runnerOptions.map((entry) => entry.value)
  if (!compatibleRunnerIds.length) {
    runnerId.value = ''
  } else if (!compatibleRunnerIds.includes(runnerId.value)) {
    runnerId.value = compatibleRunnerIds[0] || ''
  }
}, { immediate: true })

watch(
  [() => open.value, imageOptions],
  () => {
    if (!open.value) return
    const firstSelectable = imageOptions.value.find(
      (option) =>
        !option.value.startsWith('__group_') &&
        (option as SelectableImageOption).kind === 'definition',
    )
    const fallbackSelectable = imageOptions.value.find(
      (option) => !option.value.startsWith('__group_'),
    )
    const nextDefault = firstSelectable ?? fallbackSelectable
    if (!nextDefault) {
      selectedImageValue.value = ''
      return
    }
    if (!selectedImageValue.value) {
      selectedImageValue.value = nextDefault.value
    }
  },
  { immediate: true },
)

onMounted(async () => {
  if (!runnerStore.runners.length) {
    await runnerStore.fetchRunners()
  }
  await credentialStore.fetchCredentials()
  await Promise.all([
    imageStore.fetchImages(),
    imageStore.fetchImageDefinitionsWithBuilds(),
  ])
})

const effectiveQemuRunner = computed(() => {
  if (runnerId.value) {
    return runnerStore.runners.find((runner) => runner.id === runnerId.value) ?? null
  }
  return null
})

const advancedRunnerOptions = computed(() => {
  if (isFromImage.value) {
    return compatibleRunnerOptions.value
  }

  return filterRunnersByRuntime(runnerStore.onlineRunners, runtimeType.value).map(
    (runner) => ({
      value: runner.id,
      label: runner.name || runner.id.slice(0, 8),
    }),
  )
})

const qemuLimits = computed(() => {
  const runner = effectiveQemuRunner.value
  if (!runner) {
    return {
      minVcpus: 1,
      maxVcpus: 8,
      defaultVcpus: 2,
      minMemoryMb: 1024,
      maxMemoryMb: 16384,
      defaultMemoryMb: 4096,
      minDiskSizeGb: 20,
      maxDiskSizeGb: 200,
      defaultDiskSizeGb: 50,
    }
  }
  return {
    minVcpus: runner.qemu_min_vcpus,
    maxVcpus: runner.qemu_max_vcpus,
    defaultVcpus: runner.qemu_default_vcpus,
    minMemoryMb: runner.qemu_min_memory_mb,
    maxMemoryMb: runner.qemu_max_memory_mb,
    defaultMemoryMb: runner.qemu_default_memory_mb,
    minDiskSizeGb: runner.qemu_min_disk_size_gb,
    maxDiskSizeGb: runner.qemu_max_disk_size_gb,
    defaultDiskSizeGb: runner.qemu_default_disk_size_gb,
  }
})

watch([() => open.value, runtimeType, runnerId, selectedImageValue], () => {
  if (!open.value || runtimeType.value !== RuntimeType.QEMU || isCapturedClone.value) return

  const limits = qemuLimits.value
  const clampOrDefault = (value: number, min: number, max: number, fallback: number) => {
    if (Number.isNaN(value)) return fallback
    if (value < min || value > max) return fallback
    return value
  }

  qemuVcpus.value = clampOrDefault(
    qemuVcpus.value,
    limits.minVcpus,
    limits.maxVcpus,
    limits.defaultVcpus,
  )
  qemuMemoryMb.value = clampOrDefault(
    qemuMemoryMb.value,
    limits.minMemoryMb,
    limits.maxMemoryMb,
    limits.defaultMemoryMb,
  )
  qemuDiskSizeGb.value = clampOrDefault(
    qemuDiskSizeGb.value,
    limits.minDiskSizeGb,
    limits.maxDiskSizeGb,
    limits.defaultDiskSizeGb,
  )
}, { immediate: true })

function toggleCredential(id: string): void {
  if (isCapturedClone.value) return
  const credential = credentialStore.credentials.find((entry) => entry.id === id)
  if (!credential) return
  selectedCredentialIds.value = toggleWorkspaceCredentialSelection(
    selectedCredentialIds.value,
    credential,
    credentialStore.credentials,
  )
}

function addRepo(): void {
  const trimmed = repoInput.value.trim()
  if (trimmed && !repos.value.includes(trimmed)) {
    repos.value.push(trimmed)
    repoInput.value = ''
  }
}

function removeRepo(index: number): void {
  repos.value.splice(index, 1)
}

function handleRepoKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter') {
    e.preventDefault()
    addRepo()
  }
}

async function handleSubmit(): Promise<void> {
  if (!name.value.trim()) return
  const selectedOption = selectedImageOption.value
  if (!selectedOption) return

  submitting.value = true

  let success: boolean
  if (isCapturedClone.value) {
    const workspaceId = await imageStore.createWorkspaceFromImageArtifact(
      selectedOption.imageArtifact?.id || '',
      {
        name: name.value.trim(),
        credential_ids: selectedCredentialIds.value,
      },
    )
    success = !!workspaceId
  } else {
    addRepo()

    const resolvedImageId =
      selectedOption.kind === 'definition'
        ? selectedDefinitionArtifactId.value
        : selectedOption.imageArtifact?.id

    if (!resolvedImageId) {
      submitting.value = false
      return
    }

    success = await workspaceStore.createWorkspace({
      name: name.value.trim(),
      repos: repos.value,
      credential_ids: selectedCredentialIds.value,
      runner_id: runnerId.value || null,
      image_id: resolvedImageId,
      ...(runtimeType.value === RuntimeType.QEMU
        ? {
            qemu_vcpus: qemuVcpus.value,
            qemu_memory_mb: qemuMemoryMb.value,
            qemu_disk_size_gb: qemuDiskSizeGb.value,
          }
        : {}),
    })
  }

  submitting.value = false

  if (success) {
    handleClose()
  }
}

function handleClose(): void {
  open.value = false
  setTimeout(() => {
    activeTab.value = 'basic'
    name.value = ''
    selectedCredentialIds.value = []
    runnerId.value = ''
    runtimeType.value = RuntimeType.QEMU
    qemuVcpus.value = 2
    qemuMemoryMb.value = 4096
    qemuDiskSizeGb.value = 50
    repoInput.value = ''
    repos.value = []
    selectedImageValue.value = ''
  }, 200)
}

async function navigateToCredentials(): Promise<void> {
  handleClose()
  await router.push({ name: 'credentials' })
}

const isValid = computed(
  () =>
    name.value.trim().length > 0 &&
    !!selectedImageOption.value &&
    (selectedImageOption.value.kind === 'captured' || !!selectedDefinitionArtifactId.value),
)

</script>

<template>
  <UiDialog
    :open="open"
    title="Create Workspace"
    description="Provision a new workspace container. You'll choose an AI agent when starting a chat."
    @update:open="(v) => (v ? (open = true) : handleClose())"
  >
    <template #trigger>
      <UiButton @click="open = true">Create Workspace</UiButton>
    </template>

    <form class="flex flex-col gap-4" @submit.prevent="handleSubmit">
      <div class="rounded-[var(--radius-md)] border border-border bg-bg-subtle p-1">
        <div class="grid grid-cols-2 gap-1">
          <UiButton type="button" size="sm" :variant="activeTab === 'basic' ? 'secondary' : 'ghost'" @click="activeTab = 'basic'">
            Basic
          </UiButton>
          <UiButton type="button" size="sm" :variant="activeTab === 'advanced' ? 'secondary' : 'ghost'" @click="activeTab = 'advanced'">
            Advanced settings
          </UiButton>
        </div>
      </div>

      <p v-if="activeTab === 'basic'" class="text-xs text-muted-fg -mt-1">
        In most cases you can create directly from this tab. Advanced settings are optional.
      </p>

      <!-- Workspace name -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Name</label>
        <UiInput
          v-model="name"
          placeholder="My workspace"
        />
      </div>

      <!-- Image selection -->
      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">
          Select image
          <span class="text-muted-fg font-normal">(required)</span>
        </label>
        <UiSelect v-model="selectedImageValue" :options="imageOptions" />
        <p v-if="!selectedImageOption" class="text-xs text-error mt-1">
          No active organization or captured images are available.
        </p>
        <p v-if="isFromImage" class="text-xs text-muted-fg mt-1 flex items-center gap-1">
          <Camera :size="12" />
          Runtime is locked by the selected image. An image selection is required to create a workspace.
        </p>
      </div>

      <div>
        <label class="text-sm font-medium text-fg mb-1.5 block">Credentials <span class="text-muted-fg font-normal">(optional)</span></label>
        <div v-if="credentialStore.credentials.length" class="flex flex-col gap-1.5 max-h-40 overflow-y-auto">
          <button
            v-for="cred in credentialStore.credentials"
            :key="cred.id"
            type="button"
            class="flex items-center gap-2 px-3 py-2 rounded-[var(--radius-sm)] border text-left text-sm transition-colors cursor-pointer"
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
            <span v-if="cred.credential_type === 'ssh_key'" class="inline-flex items-center gap-1 text-xs text-muted-fg">
              <Key :size="10" />
              SSH Key
            </span>
            <span v-else-if="cred.target_path" class="text-xs text-muted-fg">{{ cred.target_path }}</span>
            <span v-else-if="cred.env_var_name" class="text-xs text-muted-fg">{{ cred.env_var_name }}</span>
          </button>
        </div>
        <p v-else class="text-xs text-muted-fg">
          No credentials available.
          <button type="button" class="underline cursor-pointer" @click="navigateToCredentials">Add credentials</button>
          first.
        </p>
      </div>

      <div v-if="!isCapturedClone">
        <label class="text-sm font-medium text-fg mb-1.5 block">Repositories <span class="text-muted-fg font-normal">(optional)</span></label>
        <div class="flex gap-2">
          <UiInput
            v-model="repoInput"
            placeholder="https://github.com/owner/repo"
            class="flex-1"
            @keydown="handleRepoKeydown"
          />
          <UiButton type="button" variant="outline" @click="addRepo">Add</UiButton>
        </div>
        <div v-if="repos.length" class="flex flex-wrap gap-2 mt-2">
          <span
            v-for="(repo, i) in repos"
            :key="repo"
            class="inline-flex items-center gap-1 px-2 py-1 text-xs rounded-[var(--radius-sm)] bg-muted text-fg"
          >
            {{ repo.split('/').slice(-1)[0] || repo }}
            <button
              type="button"
              class="text-muted-fg hover:text-fg cursor-pointer"
              @click="removeRepo(i)"
            >
              <X :size="12" />
            </button>
          </span>
        </div>
      </div>

      <div v-if="activeTab === 'advanced'">
        <div v-if="isCapturedClone" class="rounded-[var(--radius-md)] border border-border bg-bg-subtle p-3 text-sm text-muted-fg">
          Captured image clones keep runner, runtime and resources from the image. Advanced options are disabled.
        </div>

        <div v-else class="space-y-4">
          <div class="rounded-[var(--radius-md)] border border-border bg-bg-subtle p-3">
            <label class="text-sm font-medium text-fg mb-1.5 block">Runner</label>
            <UiSelect
              v-model="runnerId"
              :options="advancedRunnerOptions"
              :disabled="advancedRunnerOptions.length <= 1"
            />
            <p class="text-xs text-muted-fg mt-1">
              {{ isFromImage
                ? 'Only runners that have the selected image and support its runtime are available.'
                : 'Only online runners that support the selected runtime are available.' }}
            </p>
          </div>

          <div v-if="runtimeType === RuntimeType.QEMU" class="space-y-3 rounded-[var(--radius-md)] border border-border bg-bg-subtle p-3">
            <p class="text-sm font-medium text-fg">QEMU resources</p>

            <div>
              <label class="text-sm font-medium text-muted-fg mb-1 block">vCPU</label>
              <input v-model.number="qemuVcpus" type="range" class="w-full accent-primary" :min="qemuLimits.minVcpus" :max="qemuLimits.maxVcpus" step="1" />
              <input v-model.number="qemuVcpus" type="number" class="mt-1 w-full rounded border border-border bg-bg px-1.5 py-0.5 text-xs font-mono text-fg focus:outline-none focus:border-primary" :min="qemuLimits.minVcpus" :max="qemuLimits.maxVcpus" step="1" />
              <div class="flex justify-between text-xs text-muted-fg mt-1">
                <span>{{ qemuLimits.minVcpus }}</span>
                <span>{{ qemuLimits.maxVcpus }}</span>
              </div>
            </div>

            <div>
              <label class="text-sm font-medium text-muted-fg mb-1 block">RAM (MiB)</label>
              <input v-model.number="qemuMemoryMb" type="range" class="w-full accent-primary" :min="qemuLimits.minMemoryMb" :max="qemuLimits.maxMemoryMb" step="256" />
              <input v-model.number="qemuMemoryMb" type="number" class="mt-1 w-full rounded border border-border bg-bg px-1.5 py-0.5 text-xs font-mono text-fg focus:outline-none focus:border-primary" :min="qemuLimits.minMemoryMb" :max="qemuLimits.maxMemoryMb" step="256" />
              <div class="flex justify-between text-xs text-muted-fg mt-1">
                <span>{{ qemuLimits.minMemoryMb }}</span>
                <span>{{ qemuLimits.maxMemoryMb }}</span>
              </div>
            </div>

            <div>
              <label class="text-sm font-medium text-muted-fg mb-1 block">Storage (GiB)</label>
              <input v-model.number="qemuDiskSizeGb" type="range" class="w-full accent-primary" :min="qemuLimits.minDiskSizeGb" :max="qemuLimits.maxDiskSizeGb" step="1" />
              <input v-model.number="qemuDiskSizeGb" type="number" class="mt-1 w-full rounded border border-border bg-bg px-1.5 py-0.5 text-xs font-mono text-fg focus:outline-none focus:border-primary" :min="qemuLimits.minDiskSizeGb" :max="qemuLimits.maxDiskSizeGb" step="1" />
              <div class="flex justify-between text-xs text-muted-fg mt-1">
                <span>{{ qemuLimits.minDiskSizeGb }}</span>
                <span>{{ qemuLimits.maxDiskSizeGb }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Actions -->
      <div class="flex justify-end gap-2 pt-2">
        <UiButton variant="outline" type="button" @click="handleClose">Cancel</UiButton>
        <UiButton type="submit" :disabled="!isValid || submitting">
          {{
            submitting
              ? (selectedImageOption?.kind === 'captured' ? 'Cloning…' : 'Creating…')
              : (selectedImageOption?.kind === 'captured' ? 'Clone from Image' : 'Create')
          }}
        </UiButton>
      </div>
    </form>
  </UiDialog>
</template>
