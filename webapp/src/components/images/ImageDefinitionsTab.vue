<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import SettingsSection from '@/components/settings/SettingsSection.vue'
import SettingsRow from '@/components/settings/SettingsRow.vue'
import {
  Plus,
  Pencil,
  Trash2,
  ChevronDown,
  Loader2,
  Copy,
  RotateCcw,
  Layers,
} from '@lucide/vue'
import { RunnerStatus, type ImageDefinition, type Runner, type RunnerImageBuild } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { ApiRequestError, get } from '@/services/api'
import { filterRunnersByRuntime } from '@/lib/runtimeSupport'
import ImageDefinitionModal from './ImageDefinitionModal.vue'
import {
  buildNeedsPolling,
  definitionNeedsPolling,
  formatRunnerBuildSummary,
  getDefinitionActions,
  getRunnerBuildActions,
  getRunnerBuildStatusLabel,
  isDefinitionLocked,
  visibleImageDefinitions,
  type RunnerBuildAction,
  type RunnerBuildConfirmKind,
} from './imageDefinitionLifecycle'

const loading = ref(true)
const error = ref<string | null>(null)
const imageDefinitions = ref<ImageDefinition[]>([])
const runners = ref<Runner[]>([])
const buildsByDefinition = ref<Record<string, RunnerImageBuild[]>>({})
const expanded = ref<string | null>(null)
const modalOpen = ref(false)
const editing = ref<ImageDefinition | null>(null)
const actionLoading = ref<string | null>(null)
const logText = ref<string | null>(null)
const confirmDialog = ref<{
  title: string
  body: string
  confirmLabel: string
  destructive: boolean
  kind: RunnerBuildConfirmKind | 'delete_definition'
} | null>(null)
const pendingConfirm = ref<(() => Promise<void>) | null>(null)

let refreshTimer: number | null = null

const REBUILD_CONFIRM = {
  title: 'Rebuild this runner image?',
  body:
    'The image on this runner will be rebuilt. Existing workspaces keep the filesystem they were created with and are not deleted or updated. New workspaces will use the rebuilt image after the build succeeds.',
  confirmLabel: 'Rebuild',
  destructive: false,
  kind: 'rebuild' as const,
}

const REMOVE_CONFIRM = {
  title: 'Remove this runner image?',
  body:
    'The built image will be deleted from this runner. Existing workspaces are not deleted. Removal is blocked until those workspaces are gone.',
  confirmLabel: 'Remove',
  destructive: true,
  kind: 'remove' as const,
}

const DELETE_DEFINITION_CONFIRM = {
  title: 'Delete this image definition?',
  body:
    'This recipe will be deactivated and its built images will be removed from every runner. Existing workspaces are not deleted. If any workspace still uses one of those images, deletion stays blocked until those workspaces are removed.',
  confirmLabel: 'Delete definition',
  destructive: true,
  kind: 'delete_definition' as const,
}

function errorMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiRequestError) return e.message
  if (e instanceof Error) return e.message
  return fallback
}

function needsPolling(): boolean {
  if (imageDefinitions.value.some((definition) => definitionNeedsPolling(definition.status))) {
    return true
  }
  if (!expanded.value) return false
  return (buildsByDefinition.value[expanded.value] || []).some((build) =>
    buildNeedsPolling(build.status),
  )
}

function stopRefresh(): void {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer)
    refreshTimer = null
  }
}

function ensureRefresh(): void {
  stopRefresh()
  if (!needsPolling()) return
  refreshTimer = window.setInterval(async () => {
    await loadDefinitions({ quiet: true })
    if (expanded.value) await loadBuilds(expanded.value)
    if (!needsPolling()) stopRefresh()
  }, 3000)
}

async function loadDefinitions(options: { quiet?: boolean } = {}): Promise<void> {
  if (!options.quiet) {
    loading.value = true
    error.value = null
  }
  try {
    const [defs, runnerList] = await Promise.all([
      workspacesApi.listImageDefinitions(),
      get<Runner[]>('/runners/'),
    ])
    imageDefinitions.value = visibleImageDefinitions(defs)
    runners.value = runnerList
  } catch (e) {
    error.value = errorMessage(e, 'Failed to load image definitions')
  } finally {
    if (!options.quiet) loading.value = false
  }
}

async function loadBuilds(definitionId: string): Promise<void> {
  try {
    buildsByDefinition.value[definitionId] =
      await workspacesApi.listRunnerImageBuilds(definitionId)
  } catch (e) {
    error.value = errorMessage(e, 'Failed to load runner builds')
  }
}

function openCreate() {
  editing.value = null
  modalOpen.value = true
}

function openEdit(definition: ImageDefinition) {
  editing.value = definition
  modalOpen.value = true
}

async function onModalSaved(payload: Partial<ImageDefinition>): Promise<void> {
  try {
    if (editing.value) {
      await workspacesApi.updateImageDefinition(editing.value.id, payload)
    } else {
      await workspacesApi.createImageDefinition(payload)
    }
    modalOpen.value = false
    await loadDefinitions()
  } catch (e) {
    error.value = errorMessage(e, 'Failed to save image definition')
  }
}

function askConfirm(
  spec: NonNullable<typeof confirmDialog.value>,
  action: () => Promise<void>,
): void {
  confirmDialog.value = spec
  pendingConfirm.value = action
}

async function runConfirmed(): Promise<void> {
  const action = pendingConfirm.value
  confirmDialog.value = null
  pendingConfirm.value = null
  if (!action) return
  await action()
}

function cancelConfirm(): void {
  confirmDialog.value = null
  pendingConfirm.value = null
}

function requestDeleteDefinition(definition: ImageDefinition): void {
  askConfirm(DELETE_DEFINITION_CONFIRM, () => executeDeleteDefinition(definition.id))
}

async function executeDeleteDefinition(id: string): Promise<void> {
  try {
    await workspacesApi.deleteImageDefinition(id)
    await loadDefinitions()
    ensureRefresh()
  } catch (e) {
    error.value = errorMessage(e, 'Failed to delete image definition')
    await loadDefinitions({ quiet: true })
  }
}

async function restoreDefinition(id: string): Promise<void> {
  actionLoading.value = `${id}:restore`
  try {
    await workspacesApi.activateImageDefinition(id)
    await loadDefinitions()
  } catch (e) {
    error.value = errorMessage(e, 'Failed to restore image definition')
  } finally {
    actionLoading.value = null
  }
}

async function duplicateDefinition(definition: ImageDefinition): Promise<void> {
  try {
    await workspacesApi.duplicateImageDefinition(definition.id)
    await loadDefinitions()
  } catch (e) {
    error.value = errorMessage(e, 'Failed to duplicate image definition')
  }
}

function getBuild(definitionId: string, runnerId: string): RunnerImageBuild | null {
  return buildsByDefinition.value[definitionId]?.find((b) => b.runner_id === runnerId) ?? null
}

function compatibleRunners(definition: ImageDefinition): Runner[] {
  return filterRunnersByRuntime(runners.value, definition.runtime_type)
}

function isRunnerOnline(runner: Runner): boolean {
  return runner.status === RunnerStatus.ONLINE
}

function actionKey(definitionId: string, runnerId: string, action: string): string {
  return `${definitionId}:${runnerId}:${action}`
}

async function refreshRunner(definitionId: string): Promise<void> {
  await loadBuilds(definitionId)
  await loadDefinitions({ quiet: true })
  ensureRefresh()
}

async function runRunnerAction(
  definitionId: string,
  runnerId: string,
  action: string,
  work: () => Promise<void>,
): Promise<void> {
  actionLoading.value = actionKey(definitionId, runnerId, action)
  error.value = null
  try {
    await work()
    await refreshRunner(definitionId)
  } catch (e) {
    error.value = errorMessage(e, 'Failed to update runner image')
  } finally {
    actionLoading.value = null
  }
}

async function buildOnRunner(definitionId: string, runnerId: string): Promise<void> {
  await runRunnerAction(definitionId, runnerId, 'build', async () => {
    await workspacesApi.createRunnerImageBuild(definitionId, {
      runner_id: runnerId,
      activate: true,
    })
  })
}

async function patchRunner(
  definitionId: string,
  runnerId: string,
  action: 'activate' | 'deactivate' | 'rebuild',
): Promise<void> {
  await runRunnerAction(definitionId, runnerId, action, async () => {
    await workspacesApi.updateRunnerImageBuild(definitionId, runnerId, { action })
  })
}

async function removeRunnerBuild(definitionId: string, runnerId: string): Promise<void> {
  await runRunnerAction(definitionId, runnerId, 'remove', async () => {
    await workspacesApi.deleteRunnerImageBuild(definitionId, runnerId)
  })
}

async function viewLog(definitionId: string, runnerId: string): Promise<void> {
  actionLoading.value = actionKey(definitionId, runnerId, 'log')
  try {
    const result = await workspacesApi.getRunnerImageBuildLog(definitionId, runnerId)
    logText.value = result.build_log?.trim() || 'No build log available.'
  } catch (e) {
    error.value = errorMessage(e, 'Failed to load build log')
  } finally {
    actionLoading.value = null
  }
}

function handleAction(
  definition: ImageDefinition,
  runner: Runner,
  action: RunnerBuildAction,
): void {
  const definitionId = definition.id
  const runnerId = runner.id
  const run = (): Promise<void> => {
    switch (action.id) {
      case 'build':
      case 'retry':
        return buildOnRunner(definitionId, runnerId)
      case 'activate':
        return patchRunner(definitionId, runnerId, 'activate')
      case 'deactivate':
        return patchRunner(definitionId, runnerId, 'deactivate')
      case 'rebuild':
        return patchRunner(definitionId, runnerId, 'rebuild')
      case 'remove':
      case 'retry_remove':
        return removeRunnerBuild(definitionId, runnerId)
      case 'view_log':
        return viewLog(definitionId, runnerId)
    }
  }

  if (action.confirm === 'rebuild') {
    askConfirm(REBUILD_CONFIRM, run)
    return
  }
  if (action.confirm === 'remove') {
    askConfirm(REMOVE_CONFIRM, run)
    return
  }
  void run()
}

async function onExpandChange(definitionId: string, open: boolean): Promise<void> {
  if (!open) {
    if (expanded.value === definitionId) expanded.value = null
    ensureRefresh()
    return
  }
  expanded.value = definitionId
  await loadBuilds(definitionId)
  ensureRefresh()
}

function formatRuntimeType(runtime: string): string {
  if (runtime === 'qemu') return 'QEMU'
  if (!runtime) return runtime
  return runtime.charAt(0).toUpperCase() + runtime.slice(1)
}

function definitionKindLabel(definition: ImageDefinition): string {
  return definition.is_standard ? 'Standard' : 'Custom'
}

function definitionStatusLabel(status: string): string {
  if (status === 'deactivated') return 'Deactivated'
  if (status === 'pending_deletion' || status === 'deleting') return 'Removing'
  if (status === 'delete_failed') return 'Remove failed'
  return status
}

onMounted(() => {
  loadDefinitions().then(() => ensureRefresh())
})

onUnmounted(() => {
  stopRefresh()
})
</script>

<template>
  <div class="space-y-6">
    <div
      v-if="error"
      class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
    >
      {{ error }}
    </div>

    <SettingsSection
      description="An image definition is a recipe. Build it on a runner to make it available for new workspaces. Rebuilding or removing an image does not delete existing workspaces."
    >
      <template #actions>
        <Button size="sm" @click="openCreate">
          <Plus />
          New Image Definition
        </Button>
      </template>

      <div v-if="loading" class="flex justify-center py-12">
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="imageDefinitions.length === 0"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState
          :icon="Layers"
          title="No image definitions"
          description="Create a recipe, then build it on a runner to use it for new workspaces."
        />
      </div>

      <div
        v-else
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <Collapsible
          v-for="definition in imageDefinitions"
          :key="definition.id"
          :open="expanded === definition.id"
          @update:open="(open) => onExpandChange(definition.id, open)"
        >
          <SettingsRow bare-icon>
            <template #icon>
              <CollapsibleTrigger as-child>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  data-testid="image-definition-expand"
                  :aria-label="
                    expanded === definition.id ? 'Collapse runner builds' : 'Expand runner builds'
                  "
                >
                  <ChevronDown
                    class="transition-transform"
                    :class="expanded === definition.id ? 'rotate-180' : undefined"
                  />
                </Button>
              </CollapsibleTrigger>
            </template>
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-medium text-foreground">{{ definition.name }}</span>
                <Badge variant="default">{{ formatRuntimeType(definition.runtime_type) }}</Badge>
                <Badge variant="secondary">{{ definitionKindLabel(definition) }}</Badge>
                <Badge v-if="definition.status === 'deactivated'" variant="outline">
                  {{ definitionStatusLabel(definition.status) }}
                </Badge>
                <Badge
                  v-else-if="
                    definition.status === 'pending_deletion' || definition.status === 'deleting'
                  "
                  variant="destructive"
                >
                  <Loader2 :size="10" class="mr-1 inline animate-spin" />
                  {{ definitionStatusLabel(definition.status) }}
                </Badge>
                <Badge v-else-if="definition.status === 'delete_failed'" variant="destructive">
                  {{ definitionStatusLabel(definition.status) }}
                </Badge>
              </div>
              <p class="text-sm text-muted-foreground">
                {{ definition.description || 'No description' }}
              </p>
              <p class="text-xs text-muted-foreground">
                Base: {{ definition.base_distro }} ·
                {{ formatRunnerBuildSummary(definition.runner_build_summary) }}
              </p>
              <p
                v-if="definition.status === 'delete_failed' && definition.delete_last_error"
                class="text-xs text-destructive"
              >
                {{ definition.delete_last_error }}
              </p>
            </div>
            <template #actions>
              <Button
                v-if="getDefinitionActions(definition).canDuplicate"
                variant="ghost"
                size="icon-sm"
                @click="duplicateDefinition(definition)"
              >
                <Copy />
              </Button>
              <Button
                v-if="getDefinitionActions(definition).canEdit"
                variant="ghost"
                size="icon-sm"
                @click="openEdit(definition)"
              >
                <Pencil />
              </Button>
              <Button
                v-if="getDefinitionActions(definition).canDelete"
                variant="ghost"
                size="icon-sm"
                class="text-destructive hover:text-destructive"
                @click="requestDeleteDefinition(definition)"
              >
                <Trash2 />
              </Button>
              <Button
                v-if="getDefinitionActions(definition).canRestore"
                size="sm"
                variant="outline"
                :disabled="actionLoading === `${definition.id}:restore`"
                @click="restoreDefinition(definition.id)"
              >
                Restore
              </Button>
              <Button
                v-if="getDefinitionActions(definition).canRetryDelete"
                size="sm"
                variant="destructive"
                @click="requestDeleteDefinition(definition)"
              >
                <RotateCcw />
                Retry delete
              </Button>
            </template>
          </SettingsRow>

          <CollapsibleContent>
            <div class="border-t border-border px-4 pb-4 pt-3">
              <div class="overflow-x-auto">
                <table class="w-full text-sm">
                  <thead>
                    <tr class="text-left text-xs text-muted-foreground">
                      <th class="py-1.5 pr-3 font-medium">Runner</th>
                      <th class="py-1.5 pr-3 font-medium">Status</th>
                      <th class="py-1.5 font-medium">Action</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-border">
                    <tr v-for="runner in compatibleRunners(definition)" :key="runner.id">
                      <td class="py-2 pr-3">
                        <div class="flex items-center gap-2">
                          <span>{{ runner.name || runner.id.slice(0, 8) }}</span>
                          <Badge v-if="!isRunnerOnline(runner)" variant="outline">Offline</Badge>
                        </div>
                      </td>
                      <td class="py-2 pr-3">
                        <Badge
                          :variant="
                            ['pending_deletion', 'deleting', 'delete_failed', 'failed'].includes(
                              getBuild(definition.id, runner.id)?.status || '',
                            )
                              ? 'destructive'
                              : 'secondary'
                          "
                        >
                          <Loader2
                            v-if="
                              ['pending', 'building', 'pending_deletion', 'deleting'].includes(
                                getBuild(definition.id, runner.id)?.status || '',
                              )
                            "
                            :size="10"
                            class="mr-1 inline animate-spin"
                          />
                          {{
                            getRunnerBuildStatusLabel(
                              getBuild(definition.id, runner.id)?.status,
                              isRunnerOnline(runner),
                            )
                          }}
                        </Badge>
                      </td>
                      <td class="py-2">
                        <div class="flex flex-wrap items-center gap-2">
                          <Button
                            v-if="
                              ['pending', 'building'].includes(
                                getBuild(definition.id, runner.id)?.status || '',
                              )
                            "
                            size="sm"
                            variant="outline"
                            disabled
                          >
                            <Loader2 :size="12" class="animate-spin" />
                            {{
                              getBuild(definition.id, runner.id)?.status === 'pending' &&
                              !isRunnerOnline(runner)
                                ? 'Waiting for runner'
                                : 'Building…'
                            }}
                          </Button>
                          <Button
                            v-else-if="
                              ['pending_deletion', 'deleting'].includes(
                                getBuild(definition.id, runner.id)?.status || '',
                              )
                            "
                            size="sm"
                            variant="outline"
                            disabled
                          >
                            <Loader2 :size="12" class="animate-spin" />
                            Removing…
                          </Button>
                          <Button
                            v-for="action in getRunnerBuildActions(
                              getBuild(definition.id, runner.id)?.status,
                              { definitionLocked: isDefinitionLocked(definition.status) },
                            )"
                            :key="action.id"
                            size="sm"
                            :variant="
                              action.kind === 'destructive'
                                ? 'ghost'
                                : action.kind === 'ghost'
                                  ? 'ghost'
                                  : 'outline'
                            "
                            :class="action.kind === 'destructive' ? 'text-destructive' : ''"
                            :disabled="
                              Boolean(actionLoading?.startsWith(`${definition.id}:${runner.id}:`))
                            "
                            @click="handleAction(definition, runner, action)"
                          >
                            {{ action.label }}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p
                v-if="compatibleRunners(definition).length === 0"
                class="mt-3 text-xs text-muted-foreground"
              >
                No runners in this organization currently support the
                {{ formatRuntimeType(definition.runtime_type) }} runtime.
              </p>
              <p
                v-else-if="!isDefinitionLocked(definition.status)"
                class="mt-3 text-xs text-muted-foreground"
              >
                Deactivate only hides this image from new workspaces on that runner. Existing
                workspaces keep running.
              </p>
            </div>
          </CollapsibleContent>
        </Collapsible>
      </div>
    </SettingsSection>

    <ImageDefinitionModal
      :open="modalOpen"
      :image-definition="editing"
      @update:open="(v) => (modalOpen = v)"
      @saved="onModalSaved"
    />

    <Dialog :open="Boolean(confirmDialog)" @update:open="(open) => (open ? null : cancelConfirm())">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{{ confirmDialog?.title }}</DialogTitle>
          <DialogDescription>{{ confirmDialog?.body }}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" type="button" @click="cancelConfirm">Cancel</Button>
          <Button
            :variant="confirmDialog?.destructive ? 'destructive' : 'default'"
            type="button"
            @click="runConfirmed"
          >
            {{ confirmDialog?.confirmLabel }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog :open="logText !== null" @update:open="(open) => (open ? null : (logText = null))">
      <DialogContent class="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Build log</DialogTitle>
          <DialogDescription>Latest output from the runner image build.</DialogDescription>
        </DialogHeader>
        <pre class="max-h-80 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">{{
          logText
        }}</pre>
        <DialogFooter>
          <Button variant="outline" type="button" @click="logText = null">Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
