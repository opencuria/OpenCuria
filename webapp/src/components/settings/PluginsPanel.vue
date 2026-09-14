<!--
  PluginsPanel — org-visible plugin catalog (global + org-owned).

  All members can see catalog/status; only admins see mutations
  (create/edit/delete/activation toggles). Backend enforces roles.
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Pencil, Plus, Puzzle, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import SettingsSection from './SettingsSection.vue'
import SettingsRow from './SettingsRow.vue'
import PluginEditorDialog from './PluginEditorDialog.vue'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useAuthStore } from '@/stores/auth'
import { usePluginStore } from '@/stores/plugins'
import type { Plugin } from '@/types'

const authStore = useAuthStore()
const pluginStore = usePluginStore()
const router = useRouter()

const isAdmin = computed(() => authStore.isAdmin)
const activeOrgId = computed(() => authStore.activeOrganizationId)

const editorOpen = ref(false)
const editingPlugin = ref<Plugin | null>(null)
const deletingPlugin = ref<Plugin | null>(null)
const deleting = ref(false)

watch(
  activeOrgId,
  () => {
    void pluginStore.reload()
  },
  { immediate: true },
)

defineExpose({ activationDisabled, readinessLabel, readinessHint })

function readinessLabel(plugin: Plugin): string {
  const readiness = plugin.credential_readiness
  if (!readiness) return 'Unknown'
  if (readiness.ready) return 'Org credentials ready'
  return 'Org credentials missing'
}

function readinessHint(plugin: Plugin): string {
  if (plugin.credential_readiness?.ready) {
    return 'Organization credentials are configured for this plugin.'
  }
  return 'Organization credentials missing for this plugin.'
}

/**
 * Backend only blocks *enabling* an ineffective (disabled/unpublished)
 * plugin; disabling an org-active entry always succeeds. Keep the
 * switch usable for org-enabled rows so admins can always turn them off.
 */
function activationDisabled(plugin: Plugin): boolean {
  if (plugin.org_enabled) return false
  return !plugin.enabled || !plugin.published
}

function openCreate(): void {
  editingPlugin.value = null
  editorOpen.value = true
}

function openEdit(plugin: Plugin): void {
  editingPlugin.value = plugin
  editorOpen.value = true
}

async function handleToggle(plugin: Plugin, active: boolean): Promise<void> {
  await pluginStore.toggleActivation(plugin.id, active)
}

async function handleDelete(): Promise<void> {
  if (!deletingPlugin.value) return
  deleting.value = true
  const ok = await pluginStore.deletePlugin(deletingPlugin.value.id)
  deleting.value = false
  if (ok) deletingPlugin.value = null
}

async function manageCredentials(): Promise<void> {
  await router.push({ path: '/', query: { settings: 'credentials' } })
}
</script>

<template>
  <div class="space-y-6">
    <SettingsSection
      description="Reusable skill + MCP bundles. Explicit opt-in: enable a plugin for this organization, then attach it to workspaces."
    >
      <template v-if="isAdmin" #actions>
        <Button size="sm" data-testid="plugin-create" @click="openCreate">
          <Plus />
          New Plugin
        </Button>
      </template>

      <div
        v-if="pluginStore.loading && !pluginStore.plugins.length"
        class="flex justify-center py-12"
      >
        <LoadingSpinner :size="24" />
      </div>

      <div
        v-else-if="pluginStore.error"
        class="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        data-testid="plugins-error"
      >
        {{ pluginStore.error }}
      </div>

      <div
        v-else-if="!pluginStore.plugins.length"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState
          :icon="Puzzle"
          title="No plugins yet"
          description="Global plugins appear here automatically; admins can create organization plugins."
        />
      </div>

      <div v-else class="space-y-6">
        <section v-if="pluginStore.globalPlugins.length" aria-label="Global plugins">
          <h3 class="mb-2 text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            OpenCuria ({{ pluginStore.globalPlugins.length }})
          </h3>
          <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
            <SettingsRow v-for="plugin in pluginStore.globalPlugins" :key="plugin.id">
              <template #icon>
                <Puzzle :size="16" />
              </template>
              <div class="min-w-0 space-y-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-sm font-medium text-foreground">{{ plugin.name }}</span>
                  <Badge variant="secondary">Global</Badge>
                  <Badge v-if="!plugin.enabled || !plugin.published" variant="outline">Unpublished</Badge>
                  <Badge v-else-if="plugin.org_enabled" variant="default">Active</Badge>
                  <Badge v-else variant="outline">Inactive</Badge>
                  <Badge
                    :variant="plugin.credential_readiness?.ready ? 'secondary' : 'destructive'"
                    :data-testid="`plugin-readiness-${plugin.id}`"
                  >
                    {{ readinessLabel(plugin) }}
                  </Badge>
                </div>
                <p v-if="plugin.description" class="text-sm text-muted-foreground line-clamp-2">
                  {{ plugin.description }}
                </p>
                <p class="text-xs text-muted-foreground">
                  {{ plugin.skills.length }} skill{{ plugin.skills.length === 1 ? '' : 's' }} ·
                  {{ plugin.mcp_servers.length }} MCP server{{ plugin.mcp_servers.length === 1 ? '' : 's' }}
                </p>
                <p
                  v-if="!plugin.credential_readiness?.ready"
                  class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
                >
                  <span>{{ readinessHint(plugin) }}</span>
                  <button
                    type="button"
                    class="underline"
                    :data-testid="`plugin-manage-credentials-${plugin.id}`"
                    @click="manageCredentials"
                  >
                    Manage organization credentials
                  </button>
                </p>
              </div>
              <template v-if="isAdmin" #actions>
                <div class="flex items-center gap-2">
                  <Switch
                    :model-value="plugin.org_enabled"
                    :disabled="activationDisabled(plugin) || pluginStore.togglingIds.includes(plugin.id)"
                    :aria-label="plugin.org_enabled ? `Disable ${plugin.name}` : `Enable ${plugin.name}`"
                    :data-testid="`plugin-toggle-${plugin.id}`"
                    @update:model-value="(v) => handleToggle(plugin, Boolean(v))"
                  />
                </div>
              </template>
            </SettingsRow>
          </div>
        </section>

        <section v-if="pluginStore.orgPlugins.length || isAdmin" aria-label="Organization plugins">
          <h3 class="mb-2 text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
            Organization ({{ pluginStore.orgPlugins.length }})
          </h3>
          <div
            v-if="!pluginStore.orgPlugins.length"
            class="overflow-hidden rounded-lg border border-dashed border-border bg-card px-4 py-6 text-center text-sm text-muted-foreground"
          >
            No organization plugins yet.
          </div>
          <div v-else class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
            <SettingsRow v-for="plugin in pluginStore.orgPlugins" :key="plugin.id">
              <template #icon>
                <Puzzle :size="16" />
              </template>
              <div class="min-w-0 space-y-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-sm font-medium text-foreground">{{ plugin.name }}</span>
                  <Badge variant="secondary">Organization</Badge>
                  <Badge v-if="!plugin.enabled || !plugin.published" variant="outline">Unpublished</Badge>
                  <Badge v-else-if="plugin.org_enabled" variant="default">Active</Badge>
                  <Badge v-else variant="outline">Inactive</Badge>
                  <Badge
                    :variant="plugin.credential_readiness?.ready ? 'secondary' : 'destructive'"
                    :data-testid="`plugin-readiness-${plugin.id}`"
                  >
                    {{ readinessLabel(plugin) }}
                  </Badge>
                </div>
                <p v-if="plugin.description" class="text-sm text-muted-foreground line-clamp-2">
                  {{ plugin.description }}
                </p>
                <p class="text-xs text-muted-foreground">
                  {{ plugin.skills.length }} skill{{ plugin.skills.length === 1 ? '' : 's' }} ·
                  {{ plugin.mcp_servers.length }} MCP server{{ plugin.mcp_servers.length === 1 ? '' : 's' }}
                </p>
                <p
                  v-if="!plugin.credential_readiness?.ready"
                  class="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
                >
                  <span>{{ readinessHint(plugin) }}</span>
                  <button
                    type="button"
                    class="underline"
                    :data-testid="`plugin-manage-credentials-${plugin.id}`"
                    @click="manageCredentials"
                  >
                    Manage organization credentials
                  </button>
                </p>
              </div>
              <template v-if="isAdmin" #actions>
                <div class="flex items-center gap-1">
                  <Switch
                    :model-value="plugin.org_enabled"
                    :disabled="activationDisabled(plugin) || pluginStore.togglingIds.includes(plugin.id)"
                    :aria-label="plugin.org_enabled ? `Disable ${plugin.name}` : `Enable ${plugin.name}`"
                    :data-testid="`plugin-toggle-${plugin.id}`"
                    @update:model-value="(v) => handleToggle(plugin, Boolean(v))"
                  />
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    title="Edit plugin"
                    :data-testid="`plugin-edit-${plugin.id}`"
                    @click="openEdit(plugin)"
                  >
                    <Pencil />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    class="text-destructive hover:text-destructive"
                    title="Delete plugin"
                    :data-testid="`plugin-delete-${plugin.id}`"
                    @click="deletingPlugin = plugin"
                  >
                    <Trash2 />
                  </Button>
                </div>
              </template>
            </SettingsRow>
          </div>
        </section>
      </div>
    </SettingsSection>

    <PluginEditorDialog v-model:open="editorOpen" :plugin="editingPlugin" />

    <Dialog :open="!!deletingPlugin" @update:open="(v) => !v && (deletingPlugin = null)">
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete Plugin</DialogTitle>
          <DialogDescription>
            Delete {{ deletingPlugin?.name }}? This removes its skills, MCP servers, and
            requirements. This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" :disabled="deleting" @click="deletingPlugin = null">
            Cancel
          </Button>
          <Button
            variant="destructive"
            data-testid="plugin-delete-confirm"
            :disabled="deleting"
            @click="handleDelete"
          >
            {{ deleting ? 'Deleting…' : 'Delete' }}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
