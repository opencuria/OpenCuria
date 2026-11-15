<script setup lang="ts">
/**
 * ImageDefinitionsTab — image recipes, one status per row. Expanding a row
 * lists the runners with the next step for each build. Polls every 3s while
 * something builds or is removed, otherwise every 15s.
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import {
  ChevronRight,
  Copy,
  Eye,
  Layers,
  MoreHorizontal,
  Pencil,
  Plus,
  RotateCcw,
  Trash2,
} from '@lucide/vue'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from '@/components/settings/SettingsSection.vue'
import { usePolling } from '@/composables/usePolling'
import { listRunners } from '@/services/runners.api'
import * as workspacesApi from '@/services/workspaces.api'
import { useAuthStore } from '@/stores/auth'
import type {
  ImageArtifact,
  ImageDefinition,
  ImageDefinitionInput,
  Runner,
  RunnerImageBuild,
} from '@/types'
import type { DeletionTarget } from '@opencuria/client/types'
import ImageDefinitionModal from './ImageDefinitionModal.vue'
import ImageDeletionDialog from './ImageDeletionDialog.vue'
import ImageRunnerBuildRow from './ImageRunnerBuildRow.vue'
import ImageStatusBadge from './ImageStatusBadge.vue'
import {
  buildNeedsPolling,
  definitionNeedsPolling,
  getDefinitionActions,
  getDefinitionStatus,
  isDefinitionLocked,
  visibleImageDefinitions,
  type RunnerBuildAction,
} from '@opencuria/chat-core/imageDefinitionLifecycle'

const auth = useAuthStore()
const isAdmin = computed(() => auth.isAdmin)

const loaded = ref(false)
const loadError = ref<string | null>(null)
const actionError = ref<string | null>(null)
const error = computed(() => actionError.value ?? loadError.value)
const definitions = ref<ImageDefinition[]>([])
const runners = ref<Runner[]>([])
const builds = ref<Record<string, RunnerImageBuild[]>>({})
const expanded = ref<string | null>(null)
/** `definitionId` or `definitionId:runnerId` of the action in flight. */
const busyKey = ref<string | null>(null)

const editorOpen = ref(false)
const editorSaving = ref(false)
const editorError = ref<string | null>(null)
const editing = ref<ImageDefinition | null>(null)
const rebuildTarget = ref<{ definition: ImageDefinition; runner: Runner } | null>(null)
const rebuildMessage = ref('')
const log = ref<{ title: string; text: string } | null>(null)
const logOutput = ref<HTMLElement | null>(null)
const deletion = ref<{ target: DeletionTarget; name: string } | null>(null)

const inFlight = computed(
  () =>
    definitions.value.some((definition) => definitionNeedsPolling(definition)) ||
    (expanded.value !== null &&
      (builds.value[expanded.value] ?? []).some((build) => buildNeedsPolling(build))),
)
const { start } = usePolling(
  refresh,
  computed(() => (inFlight.value ? 3000 : 15000)),
)
onMounted(start)

// Running builds append at the end: open the log there.
watch(log, async (value) => {
  if (!value) return
  await nextTick()
  const output = logOutput.value
  if (output) output.scrollTop = output.scrollHeight
})

function errorMessage(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

function runnerName(runner: Runner): string {
  return runner.name || runner.id.slice(0, 8)
}

/** "Standard · ubuntu:24.04 · Python tools". */
function definitionMeta(definition: ImageDefinition): string {
  return [definition.is_standard ? 'Standard' : '', definition.base_distro, definition.description]
    .filter(Boolean)
    .join(' · ')
}

function hasMenu(definition: ImageDefinition): boolean {
  return isAdmin.value && Object.values(getDefinitionActions(definition)).some(Boolean)
}

function buildFor(definitionId: string, runnerId: string): RunnerImageBuild | null {
  return builds.value[definitionId]?.find((build) => build.runner_id === runnerId) ?? null
}

async function refresh(): Promise<void> {
  await Promise.all([loadDefinitions(), expanded.value ? loadBuilds(expanded.value) : null])
}

async function loadDefinitions(): Promise<void> {
  try {
    const [list, runnerList] = await Promise.all([
      workspacesApi.listImageDefinitions(),
      listRunners(),
    ])
    definitions.value = visibleImageDefinitions(list)
    runners.value = runnerList
    loadError.value = null
  } catch (e) {
    loadError.value = errorMessage(e, 'Could not load image definitions.')
  } finally {
    loaded.value = true
  }
}

async function loadBuilds(definitionId: string): Promise<void> {
  try {
    const list = await workspacesApi.listRunnerImageBuilds(definitionId)
    builds.value = { ...builds.value, [definitionId]: list }
  } catch (e) {
    loadError.value = errorMessage(e, 'Could not load runner builds.')
  }
}

async function setExpanded(definition: ImageDefinition, open: boolean): Promise<void> {
  if (!open) {
    if (expanded.value === definition.id) expanded.value = null
    return
  }
  expanded.value = definition.id
  await loadBuilds(definition.id)
}

/** Run one action, show its error, then reload what it changed. */
async function run(
  key: string,
  work: () => Promise<unknown>,
  definitionId?: string,
): Promise<void> {
  busyKey.value = key
  actionError.value = null
  try {
    await work()
  } catch (e) {
    actionError.value = errorMessage(e, 'The action failed.')
  } finally {
    busyKey.value = null
  }
  await Promise.all([loadDefinitions(), definitionId ? loadBuilds(definitionId) : null])
}

function openCreate(): void {
  editing.value = null
  editorError.value = null
  editorOpen.value = true
}

function openEdit(definition: ImageDefinition): void {
  editing.value = definition
  editorError.value = null
  editorOpen.value = true
}

async function onEditorSaved(payload: ImageDefinitionInput): Promise<void> {
  if (editorSaving.value) return
  editorSaving.value = true
  editorError.value = null
  try {
    const current = editing.value
    const saved = current
      ? await workspacesApi.updateImageDefinition(current.id, payload)
      : await workspacesApi.createImageDefinition(payload)
    editorOpen.value = false
    await loadDefinitions()
    // A saved recipe is built next: open it so Build or Rebuild is right there.
    if (saved) await setExpanded(saved, true)
  } catch (e) {
    editorError.value = errorMessage(e, 'Could not save the image definition.')
  } finally {
    editorSaving.value = false
  }
}

/** Copy a recipe and open the copy for editing. */
async function duplicate(definition: ImageDefinition): Promise<void> {
  busyKey.value = definition.id
  actionError.value = null
  try {
    const copy = await workspacesApi.duplicateImageDefinition(definition.id)
    await loadDefinitions()
    openEdit(copy)
  } catch (e) {
    actionError.value = errorMessage(e, 'Could not duplicate the image definition.')
  } finally {
    busyKey.value = null
  }
}

function activate(definition: ImageDefinition): void {
  void run(definition.id, () => workspacesApi.activateImageDefinition(definition.id))
}

function requestDelete(definition: ImageDefinition): void {
  deletion.value = {
    target: { target_type: 'definition', target_id: definition.id },
    name: definition.name,
  }
}

function requestDeleteVersion(definition: ImageDefinition, version: ImageArtifact): void {
  deletion.value = {
    target: { target_type: 'image', target_id: version.id },
    name: version.version ? `${definition.name} · v${version.version}` : definition.name,
  }
}

async function showLog(definition: ImageDefinition, runner: Runner): Promise<void> {
  busyKey.value = `${definition.id}:${runner.id}`
  actionError.value = null
  try {
    const result = await workspacesApi.getRunnerImageBuildLog(definition.id, runner.id)
    log.value = {
      title: `${definition.name} on ${runnerName(runner)}`,
      text: result.build_log?.trim() || 'No output yet.',
    }
  } catch (e) {
    actionError.value = errorMessage(e, 'Could not load the build log.')
  } finally {
    busyKey.value = null
  }
}

function onRunnerAction(
  definition: ImageDefinition,
  runner: Runner,
  action: RunnerBuildAction,
): void {
  const key = `${definition.id}:${runner.id}`
  switch (action.id) {
    case 'build':
    case 'retry':
      void run(
        key,
        () =>
          workspacesApi.createRunnerImageBuild(definition.id, {
            runner_id: runner.id,
            activate: true,
          }),
        definition.id,
      )
      return
    case 'activate':
    case 'deactivate': {
      const change = action.id
      void run(
        key,
        () => workspacesApi.updateRunnerImageBuild(definition.id, runner.id, { action: change }),
        definition.id,
      )
      return
    }
    case 'rebuild':
      rebuildMessage.value = ''
      rebuildTarget.value = { definition, runner }
      return
    case 'view_log':
      void showLog(definition, runner)
      return
    case 'remove':
    case 'retry_remove': {
      const build = buildFor(definition.id, runner.id)
      if (build) {
        deletion.value = {
          target: { target_type: 'assignment', target_id: build.id },
          name: `${definition.name} on ${runnerName(runner)}`,
        }
      }
      return
    }
  }
}

async function confirmRebuild(): Promise<void> {
  const target = rebuildTarget.value
  if (!target) return
  rebuildTarget.value = null
  const message = rebuildMessage.value.trim()
  await run(
    `${target.definition.id}:${target.runner.id}`,
    () =>
      workspacesApi.updateRunnerImageBuild(target.definition.id, target.runner.id, {
        action: 'rebuild',
        ...(message ? { message } : {}),
      }),
    target.definition.id,
  )
}

async function onDeletionRequested(): Promise<void> {
  await refresh()
}
</script>

<template>
  <div class="space-y-6">
    <SettingsSection description="Build a recipe on a runner to start new workspaces from it.">
      <template v-if="isAdmin" #actions>
        <Button size="sm" data-testid="image-definition-create" @click="openCreate">
          <Plus />
          New Definition
        </Button>
      </template>

      <p
        v-if="error"
        role="alert"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
      >
        {{ error }}
      </p>

      <div v-if="!loaded" class="flex justify-center py-12">
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="!definitions.length && !loadError"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState :icon="Layers" title="No image definitions yet" />
      </div>

      <div
        v-else-if="definitions.length"
        class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
      >
        <Collapsible
          v-for="definition in definitions"
          :key="definition.id"
          :open="expanded === definition.id"
          data-testid="image-definition-row"
          @update:open="(open) => setExpanded(definition, open)"
        >
          <div class="flex items-center gap-1 pr-3 transition-colors hover:bg-muted/30">
            <CollapsibleTrigger
              class="flex min-w-0 flex-1 items-center gap-3 rounded-lg py-3 pl-4 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
              data-testid="image-definition-toggle"
            >
              <ChevronRight
                class="size-4 shrink-0 text-muted-foreground transition-transform"
                :class="{ 'rotate-90': expanded === definition.id }"
                aria-hidden="true"
              />
              <span class="min-w-0 space-y-0.5">
                <span class="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span class="truncate text-sm font-medium text-foreground">
                    {{ definition.name }}
                  </span>
                  <ImageStatusBadge :status="getDefinitionStatus(definition)" />
                </span>
                <span class="block truncate text-xs text-muted-foreground">
                  {{ definitionMeta(definition) }}
                </span>
              </span>
            </CollapsibleTrigger>

            <DropdownMenu v-if="hasMenu(definition)">
              <DropdownMenuTrigger as-child>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  class="text-muted-foreground"
                  :disabled="busyKey === definition.id"
                  :aria-label="`More actions for ${definition.name}`"
                  title="More actions"
                  data-testid="image-definition-menu"
                >
                  <MoreHorizontal />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" class="w-44">
                <DropdownMenuItem
                  v-if="getDefinitionActions(definition).canEdit"
                  data-testid="image-definition-edit"
                  @select="openEdit(definition)"
                >
                  <Pencil />
                  Edit
                </DropdownMenuItem>
                <DropdownMenuItem
                  v-if="getDefinitionActions(definition).canDuplicate"
                  data-testid="image-definition-duplicate"
                  @select="duplicate(definition)"
                >
                  <Copy />
                  Duplicate
                </DropdownMenuItem>
                <DropdownMenuItem
                  v-if="getDefinitionActions(definition).canEnable"
                  data-testid="image-definition-enable"
                  @select="activate(definition)"
                >
                  <Eye />
                  Enable
                </DropdownMenuItem>
                <DropdownMenuItem
                  v-if="getDefinitionActions(definition).canRestore"
                  data-testid="image-definition-restore"
                  @select="activate(definition)"
                >
                  <RotateCcw />
                  Restore
                </DropdownMenuItem>
                <template
                  v-if="
                    getDefinitionActions(definition).canDelete ||
                    getDefinitionActions(definition).canRetryDelete
                  "
                >
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    variant="destructive"
                    data-testid="image-definition-delete"
                    @select="requestDelete(definition)"
                  >
                    <Trash2 />
                    {{
                      getDefinitionActions(definition).canRetryDelete ? 'Retry delete' : 'Delete'
                    }}
                  </DropdownMenuItem>
                </template>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>

          <p
            v-if="definition.status === 'delete_failed' && definition.delete_last_error"
            class="pb-3 pl-11 pr-4 text-xs text-destructive"
          >
            {{ definition.delete_last_error }}
          </p>

          <CollapsibleContent>
            <div class="border-t border-border/60 bg-muted/20 py-1">
              <div v-if="!(definition.id in builds)" class="flex justify-center py-3">
                <LoadingSpinner :size="16" />
              </div>
              <template v-else>
                <ImageRunnerBuildRow
                  v-for="runner in runners"
                  :key="runner.id"
                  :runner="runner"
                  :build="buildFor(definition.id, runner.id)"
                  :can-manage="isAdmin"
                  :locked="isDefinitionLocked(definition.status)"
                  :busy="busyKey === `${definition.id}:${runner.id}`"
                  @action="(action) => onRunnerAction(definition, runner, action)"
                  @delete-version="(version) => requestDeleteVersion(definition, version)"
                />
                <p v-if="!runners.length" class="py-2.5 pl-11 pr-4 text-sm text-muted-foreground">
                  No runners yet.
                </p>
              </template>
            </div>
          </CollapsibleContent>
        </Collapsible>
      </div>
    </SettingsSection>

    <ImageDeletionDialog
      :target="deletion?.target ?? null"
      :name="deletion?.name"
      @close="deletion = null"
      @requested="onDeletionRequested"
    />
    <ImageDefinitionModal
      :open="editorOpen"
      :image-definition="editing"
      :saving="editorSaving"
      :error="editorError"
      @update:open="(value) => (editorOpen = value)"
      @saved="onEditorSaved"
    />

    <Dialog
      :open="rebuildTarget !== null"
      @update:open="(open) => (open ? null : (rebuildTarget = null))"
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Rebuild on {{ rebuildTarget ? runnerName(rebuildTarget.runner) : '' }}?
          </DialogTitle>
          <DialogDescription
            >Builds a new version. Existing workspaces keep theirs.</DialogDescription
          >
        </DialogHeader>
        <DialogBody>
          <form id="image-rebuild-form" @submit.prevent="confirmRebuild">
            <Input
              v-model="rebuildMessage"
              maxlength="500"
              placeholder="What changed? (optional)"
              aria-label="What changed?"
              data-testid="rebuild-message-input"
            />
          </form>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" type="button" @click="rebuildTarget = null">Cancel</Button>
          <Button type="submit" form="image-rebuild-form" data-testid="rebuild-confirm">
            Rebuild
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>

    <Dialog :open="log !== null" @update:open="(open) => (open ? null : (log = null))">
      <DialogContent class="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Build log</DialogTitle>
          <DialogDescription>{{ log?.title }}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <pre
            ref="logOutput"
            class="max-h-96 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-xs"
            >{{ log?.text }}</pre
          >
        </DialogBody>
      </DialogContent>
    </Dialog>
  </div>
</template>
