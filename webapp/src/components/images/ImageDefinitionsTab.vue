<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { UiBadge, UiButton, UiCard, UiCardContent, UiSpinner } from '@/components/ui'
import { Plus, Pencil, Trash2, ChevronsDownUp, ChevronsUpDown, RefreshCcw, Loader2, Copy } from 'lucide-vue-next'
import type { ImageDefinition, Runner, RunnerImageBuild } from '@/types'
import * as workspacesApi from '@/services/workspaces.api'
import { get } from '@/services/api'
import { filterRunnersByRuntime } from '@/lib/runtimeSupport'
import ImageDefinitionModal from './ImageDefinitionModal.vue'

const loading = ref(true)
const error = ref<string | null>(null)
const imageDefinitions = ref<ImageDefinition[]>([])
const runners = ref<Runner[]>([])
const buildsByDefinition = ref<Record<string, RunnerImageBuild[]>>({})
const expanded = ref<string | null>(null)
const modalOpen = ref(false)
const editing = ref<ImageDefinition | null>(null)
const actionLoading = ref<string | null>(null)
let refreshTimer: number | null = null

async function loadDefinitions(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [defs, runnerList] = await Promise.all([
      workspacesApi.listImageDefinitions(),
      get<Runner[]>('/runners/'),
    ])
    imageDefinitions.value = defs
    runners.value = runnerList
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load image definitions'
  } finally {
    loading.value = false
  }
}

async function loadBuilds(definitionId: string): Promise<void> {
  try {
    buildsByDefinition.value[definitionId] = await workspacesApi.listRunnerImageBuilds(definitionId)
  } catch {
    buildsByDefinition.value[definitionId] = []
  }
}

function stopBuildRefresh(): void {
  if (refreshTimer !== null) {
    window.clearInterval(refreshTimer)
    refreshTimer = null
  }
}

function ensureBuildRefresh(definitionId: string): void {
  stopBuildRefresh()
  refreshTimer = window.setInterval(async () => {
    const builds = buildsByDefinition.value[definitionId] || []
    const hasActiveBuild = builds.some((build) => ['pending', 'building'].includes(build.status))
    if (!hasActiveBuild) {
      stopBuildRefresh()
      return
    }
    await loadBuilds(definitionId)
  }, 3000)
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
    error.value = e instanceof Error ? e.message : 'Failed to save image definition'
  }
}

async function removeDefinition(id: string): Promise<void> {
  if (!confirm('Delete this image definition? This will deactivate it and initiate deletion of all runner builds.')) return
  try {
    await workspacesApi.deleteImageDefinition(id)
    await loadDefinitions()
  } catch (e: any) {
    if (e?.response?.status === 409) {
      error.value = e?.response?.data?.detail || 'Cannot delete: definition is in use or already being deleted.'
    } else {
      error.value = e instanceof Error ? e.message : 'Failed to delete image definition'
    }
  }
}

async function duplicateDefinition(definition: ImageDefinition): Promise<void> {
  try {
    await workspacesApi.duplicateImageDefinition(definition.id)
    await loadDefinitions()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to duplicate image definition'
  }
}

function getBuild(definitionId: string, runnerId: string): RunnerImageBuild | null {
  return buildsByDefinition.value[definitionId]?.find((b) => b.runner_id === runnerId) ?? null
}

function compatibleRunners(definition: ImageDefinition): Runner[] {
  return filterRunnersByRuntime(runners.value, definition.runtime_type)
}

async function assignOrActivate(definitionId: string, runnerId: string): Promise<void> {
  actionLoading.value = `${definitionId}:${runnerId}:assign`
  try {
    await workspacesApi.createRunnerImageBuild(definitionId, { runner_id: runnerId, activate: true })
    await loadBuilds(definitionId)
    ensureBuildRefresh(definitionId)
  } finally {
    actionLoading.value = null
  }
}

async function deactivate(definitionId: string, runnerId: string): Promise<void> {
  actionLoading.value = `${definitionId}:${runnerId}:deactivate`
  try {
    await workspacesApi.updateRunnerImageBuild(definitionId, runnerId, { action: 'deactivate' })
    await loadBuilds(definitionId)
  } finally {
    actionLoading.value = null
  }
}

async function rebuild(definitionId: string, runnerId: string): Promise<void> {
  actionLoading.value = `${definitionId}:${runnerId}:rebuild`
  try {
    await workspacesApi.updateRunnerImageBuild(definitionId, runnerId, { action: 'rebuild' })
    await loadBuilds(definitionId)
    ensureBuildRefresh(definitionId)
  } finally {
    actionLoading.value = null
  }
}

async function activate(definitionId: string, runnerId: string): Promise<void> {
  actionLoading.value = `${definitionId}:${runnerId}:activate`
  try {
    await workspacesApi.updateRunnerImageBuild(definitionId, runnerId, { action: 'activate' })
    await loadBuilds(definitionId)
    ensureBuildRefresh(definitionId)
  } finally {
    actionLoading.value = null
  }
}

async function viewLog(definitionId: string, runnerId: string): Promise<void> {
  actionLoading.value = `${definitionId}:${runnerId}:log`
  try {
    const result = await workspacesApi.getRunnerImageBuildLog(definitionId, runnerId)
    const log = result.build_log?.trim() || 'No build log available.'
    alert(log)
  } finally {
    actionLoading.value = null
  }
}

async function toggleExpand(definitionId: string): Promise<void> {
  expanded.value = expanded.value === definitionId ? null : definitionId
  if (expanded.value === definitionId) {
    await loadBuilds(definitionId)
    if (
      (buildsByDefinition.value[definitionId] || []).some((build) =>
        ['pending', 'building'].includes(build.status),
      )
    ) {
      ensureBuildRefresh(definitionId)
    } else {
      stopBuildRefresh()
    }
  } else {
    stopBuildRefresh()
  }
}

function summary(definitionId: string): string {
  const builds = buildsByDefinition.value[definitionId] || []
  const active = builds.filter((b) => b.status === 'active').length
  const building = builds.filter((b) => b.status === 'building' || b.status === 'pending').length
  const failed = builds.filter((b) => b.status === 'failed').length
  return `${active} active, ${building} building, ${failed} failed`
}

onMounted(() => {
  loadDefinitions()
})

onUnmounted(() => {
  stopBuildRefresh()
})
</script>

<template>
  <div class="space-y-4">
    <div class="flex justify-between items-center">
      <p class="text-sm text-muted-fg">Define reusable images and activate them per runner.</p>
      <UiButton size="sm" @click="openCreate">
        <Plus :size="14" />
        New Image Definition
      </UiButton>
    </div>

    <div v-if="loading" class="flex justify-center py-10"><UiSpinner :size="24" /></div>

    <div v-else-if="error" class="rounded-[var(--radius-md)] border border-error/30 bg-error-muted px-4 py-3 text-sm text-error">
      {{ error }}
    </div>

    <div v-else class="space-y-2">
      <UiCard v-for="definition in imageDefinitions" :key="definition.id">
        <UiCardContent>
          <div class="flex items-center gap-3">
            <button type="button" class="text-muted-fg hover:text-fg" @click="toggleExpand(definition.id)">
              <ChevronsUpDown v-if="expanded !== definition.id" :size="14" />
              <ChevronsDownUp v-else :size="14" />
            </button>
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2">
                <span class="font-medium text-fg">{{ definition.name }}</span>
                <UiBadge variant="info">{{ definition.runtime_type }}</UiBadge>
                <UiBadge :variant="definition.is_standard ? 'muted' : 'success'">
                  {{ definition.is_standard ? 'standard' : 'custom' }}
                </UiBadge>
                <UiBadge v-if="definition.status === 'deactivated'" variant="warning">deactivated</UiBadge>
                <UiBadge v-else-if="definition.status === 'pending_deletion'" variant="error">
                  <Loader2 :size="10" class="inline animate-spin mr-1" />pending deletion
                </UiBadge>
                <UiBadge v-else-if="definition.status === 'deleting'" variant="error">
                  <Loader2 :size="10" class="inline animate-spin mr-1" />deleting
                </UiBadge>
                <UiBadge v-else-if="definition.status === 'deleted'" variant="muted">deleted</UiBadge>
              </div>
              <p class="text-xs text-muted-fg truncate">{{ definition.description || 'No description' }}</p>
              <p class="text-xs text-muted-fg">Base: {{ definition.base_distro }} · {{ summary(definition.id) }}</p>
            </div>
            <UiButton variant="ghost" size="icon-sm" :disabled="['pending_deletion', 'deleting', 'deleted'].includes(definition.status || 'active')" @click="duplicateDefinition(definition)">
              <Copy :size="14" />
            </UiButton>
            <UiButton v-if="!definition.is_standard" variant="ghost" size="icon-sm" :disabled="['pending_deletion', 'deleting', 'deleted'].includes(definition.status || 'active')" @click="openEdit(definition)">
              <Pencil :size="14" />
            </UiButton>
            <UiButton
              v-if="!definition.is_standard"
              variant="ghost"
              size="icon-sm"
              class="text-error"
              :disabled="['pending_deletion', 'deleting', 'deleted'].includes(definition.status || 'active')"
              @click="removeDefinition(definition.id)"
            >
              <Trash2 :size="14" />
            </UiButton>
          </div>

          <div v-if="expanded === definition.id" class="mt-4 border-t border-border pt-3">
            <div class="overflow-x-auto">
              <table class="w-full text-sm">
                <thead>
                  <tr class="text-left text-muted-fg">
                    <th class="py-1 pr-3">Runner</th>
                    <th class="py-1 pr-3">Status</th>
                    <th class="py-1">Action</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="runner in compatibleRunners(definition)" :key="runner.id" class="border-t border-border/40">
                    <td class="py-2 pr-3">{{ runner.name || runner.id.slice(0, 8) }}</td>
                    <td class="py-2 pr-3">
                      <UiBadge v-if="getBuild(definition.id, runner.id)" :variant="['pending_deletion', 'deleting', 'delete_failed'].includes(getBuild(definition.id, runner.id)?.status || '') ? 'error' : 'muted'">
                        <Loader2
                          v-if="['pending', 'building', 'pending_deletion', 'deleting'].includes(getBuild(definition.id, runner.id)?.status || '')"
                          :size="10"
                          class="inline animate-spin mr-1"
                        />
                        {{ getBuild(definition.id, runner.id)?.status }}
                      </UiBadge>
                      <span v-else class="text-xs text-muted-fg">not assigned</span>
                    </td>
                    <td class="py-2">
                      <div class="flex items-center gap-2">
                        <UiButton
                          v-if="!getBuild(definition.id, runner.id)"
                          size="sm"
                          variant="outline"
                          :disabled="actionLoading === `${definition.id}:${runner.id}:assign`"
                          @click="assignOrActivate(definition.id, runner.id)"
                        >Assign & Activate</UiButton>
                        <UiButton
                          v-else-if="getBuild(definition.id, runner.id)?.status === 'active'"
                          size="sm"
                          variant="outline"
                          :disabled="actionLoading === `${definition.id}:${runner.id}:deactivate`"
                          @click="deactivate(definition.id, runner.id)"
                        >Deactivate</UiButton>
                        <UiButton
                          v-else-if="getBuild(definition.id, runner.id)?.status === 'deactivated'"
                          size="sm"
                          variant="outline"
                          :disabled="actionLoading === `${definition.id}:${runner.id}:activate`"
                          @click="activate(definition.id, runner.id)"
                        >Activate</UiButton>
                        <UiButton
                          v-else-if="getBuild(definition.id, runner.id)?.status === 'failed'"
                          size="sm"
                          variant="outline"
                          :disabled="actionLoading === `${definition.id}:${runner.id}:rebuild`"
                          @click="rebuild(definition.id, runner.id)"
                        >
                          <RefreshCcw :size="12" />
                          Rebuild
                        </UiButton>
                        <UiButton
                          v-if="getBuild(definition.id, runner.id)?.status === 'failed'"
                          size="sm"
                          variant="ghost"
                          :disabled="actionLoading === `${definition.id}:${runner.id}:log`"
                          @click="viewLog(definition.id, runner.id)"
                        >View Log</UiButton>
                        <UiButton
                          v-else-if="getBuild(definition.id, runner.id)?.status === 'pending' || getBuild(definition.id, runner.id)?.status === 'building'"
                          size="sm"
                          variant="outline"
                          disabled
                        >
                          <Loader2 :size="12" class="animate-spin" />
                          Building…
                        </UiButton>
                        <UiButton
                          v-else
                          size="sm"
                          variant="outline"
                          :disabled="actionLoading === `${definition.id}:${runner.id}:rebuild`"
                          @click="rebuild(definition.id, runner.id)"
                        >
                          <RefreshCcw :size="12" />
                          Rebuild / Activate
                        </UiButton>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-if="compatibleRunners(definition).length === 0" class="mt-3 text-xs text-muted-fg">
              No runners in this organization currently support the {{ definition.runtime_type }} runtime.
            </p>
          </div>
        </UiCardContent>
      </UiCard>

      <div v-if="imageDefinitions.length === 0" class="py-10 text-center text-sm text-muted-fg">
        No image definitions found.
      </div>
    </div>

    <ImageDefinitionModal
      :open="modalOpen"
      :image-definition="editing"
      @update:open="(v) => (modalOpen = v)"
      @saved="onModalSaved"
    />
  </div>
</template>
