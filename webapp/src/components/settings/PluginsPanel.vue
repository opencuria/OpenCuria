<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Pencil, Plus, Puzzle, Trash2 } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import SettingsSection from './SettingsSection.vue'
import PluginEditorDialog from './PluginEditorDialog.vue'
import { usePluginStore } from '@/stores/plugins'
import { useAuthStore } from '@/stores/auth'
import type { Plugin } from '@/types'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

const props = defineProps<{ initialPluginId?: string; contextVersion?: number }>()
const pluginStore = usePluginStore()
const authStore = useAuthStore()
const router = useRouter()
const route = useRoute()
const isAdmin = computed(() => authStore.isAdmin)
const selectedId = ref<string | null>(
  props.initialPluginId ?? (typeof route.query.plugin === 'string' ? route.query.plugin : null),
)
const selectedPlugin = computed(
  () => pluginStore.plugins.find((plugin) => plugin.id === selectedId.value) ?? null,
)
const detailUnavailable = computed(
  () =>
    selectedId.value !== null &&
    !pluginStore.loading &&
    pluginStore.loadedOrgId === authStore.activeOrganizationId &&
    !selectedPlugin.value,
)
const editorOpen = ref(false)
const editingPlugin = ref<Plugin | null>(null)
const deletingPlugin = ref<Plugin | null>(null)
const deleting = ref(false)
const expandedSkills = ref<string[]>([])

watch(
  () => authStore.activeOrganizationId,
  () => {
    // Keep the requested detail while its new-organization catalog is loading.
    void pluginStore.reload()
  },
  { immediate: true },
)
watch(
  () => route.query.plugin,
  (id) => {
    if (typeof id === 'string') selectedId.value = id
    else if (selectedId.value && !props.initialPluginId) selectedId.value = null
  },
)
watch(
  () => [props.initialPluginId, props.contextVersion] as const,
  ([id]) => {
    if (id) {
      selectedId.value = id
      if (route.query.plugin !== id)
        void router
          .replace({ path: route.path, query: { ...route.query, plugin: id } })
          .catch(() => undefined)
    }
  },
)
function openPlugin(plugin: Plugin): void {
  selectedId.value = plugin.id
  expandedSkills.value = []
  void router
    .replace({ path: route.path, query: { ...route.query, plugin: plugin.id } })
    .catch(() => undefined)
}
function backToList(): void {
  selectedId.value = null
  const query = { ...route.query }
  delete query.plugin
  void router.replace({ path: route.path, query }).catch(() => undefined)
}
function addCredential(serviceId: string): void {
  void router.push({ path: '/', query: { settings: 'credentials', add_credential: serviceId } })
}
function openEdit(plugin: Plugin): void {
  editingPlugin.value = plugin
  editorOpen.value = true
}
function createPlugin(): void {
  editingPlugin.value = null
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
  if (ok) {
    selectedId.value = null
    deletingPlugin.value = null
  }
}
function toggleSkill(id: string): void {
  expandedSkills.value = expandedSkills.value.includes(id)
    ? expandedSkills.value.filter((entry) => entry !== id)
    : [...expandedSkills.value, id]
}
function mcpEndpoint(server: Plugin['mcp_servers'][number]): string {
  return server.transport === 'stdio'
    ? [server.command, ...server.args].filter(Boolean).join(' ')
    : server.url
}

function availabilityLabel(plugin: Plugin): string {
  if (!plugin.enabled || !plugin.published) return 'Unpublished'
  return plugin.org_enabled ? 'Active' : 'Inactive'
}

function transportLabel(transport: string): string {
  return transport === 'streamable_http'
    ? 'Streamable HTTP'
    : transport === 'stdio'
      ? 'STDIO'
      : transport.toUpperCase()
}

function credentialTypeLabel(type: string): string {
  return type === 'mcp_oauth' ? 'OAuth' : type.toUpperCase()
}
</script>

<template>
  <div class="space-y-6">
    <SettingsSection
      :description="
        selectedPlugin
          ? 'Plugin capabilities and credential dependencies.'
          : 'Browse reusable skills and MCP server bundles available to this organization.'
      "
    >
      <template v-if="isAdmin && !selectedPlugin" #actions>
        <Button size="sm" data-testid="plugin-create" @click="createPlugin"
          ><Plus /> New Plugin</Button
        >
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
      <div v-else-if="selectedPlugin" class="space-y-6" data-testid="plugin-detail">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div class="flex min-w-0 items-start gap-3">
            <Button
              variant="outline"
              size="sm"
              aria-label="Back to plugins"
              data-testid="plugin-back"
              @click="backToList"
              ><ArrowLeft /> Back</Button
            >
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <h2 class="text-lg font-semibold">{{ selectedPlugin.name }}</h2>
                <Badge variant="secondary">{{
                  selectedPlugin.is_global ? 'Global' : 'Organization'
                }}</Badge>
              </div>
              <p class="whitespace-pre-wrap text-sm text-muted-foreground">
                {{ selectedPlugin.description || 'No description provided.' }}
              </p>
            </div>
          </div>
          <div v-if="isAdmin" class="flex items-center gap-2">
            <span class="text-xs text-muted-foreground">{{
              selectedPlugin.org_enabled ? 'Active for organization' : 'Inactive for organization'
            }}</span>
            <Switch
              :model-value="selectedPlugin.org_enabled"
              :disabled="
                (!selectedPlugin.org_enabled &&
                  (!selectedPlugin.enabled || !selectedPlugin.published)) ||
                pluginStore.togglingIds.includes(selectedPlugin.id)
              "
              :aria-label="
                selectedPlugin.org_enabled
                  ? `Disable ${selectedPlugin.name}`
                  : `Enable ${selectedPlugin.name}`
              "
              :data-testid="`plugin-toggle-${selectedPlugin.id}`"
              @update:model-value="(value) => handleToggle(selectedPlugin!, Boolean(value))"
            />
            <template v-if="!selectedPlugin.is_global">
              <Button
                variant="outline"
                size="sm"
                data-testid="plugin-edit-detail"
                @click="openEdit(selectedPlugin!)"
                ><Pencil /> Edit</Button
              >
              <Button
                variant="destructive"
                size="sm"
                data-testid="plugin-delete-detail"
                @click="deletingPlugin = selectedPlugin"
                ><Trash2 /> Delete</Button
              >
            </template>
          </div>
        </div>
        <p
          v-if="!selectedPlugin.enabled || !selectedPlugin.published"
          class="text-xs text-muted-foreground"
        >
          This plugin is not currently published for workspace use.
        </p>

        <section aria-label="Skills" class="space-y-2">
          <h3 class="text-sm font-semibold">Skills ({{ selectedPlugin.skills.length }})</h3>
          <div
            v-if="!selectedPlugin.skills.length"
            class="rounded-md border border-dashed p-3 text-sm text-muted-foreground"
          >
            No skills.
          </div>
          <div
            v-for="skill in [...selectedPlugin.skills].sort((a, b) => a.position - b.position)"
            :key="skill.id"
            class="rounded-md border border-border"
          >
            <button
              type="button"
              class="flex w-full items-center justify-between gap-3 p-3 text-left text-sm font-medium"
              :aria-expanded="expandedSkills.includes(skill.id)"
              @click="toggleSkill(skill.id)"
            >
              <span>{{ skill.name }}</span
              ><span class="text-xs text-muted-foreground">{{
                expandedSkills.includes(skill.id) ? 'Hide' : 'Show'
              }}</span>
            </button>
            <pre
              v-if="expandedSkills.includes(skill.id)"
              class="whitespace-pre-wrap break-words border-t border-border px-3 py-3 font-sans text-sm text-muted-foreground"
              >{{ skill.body }}</pre
            >
          </div>
        </section>

        <section aria-label="MCP servers" class="space-y-2">
          <h3 class="text-sm font-semibold">
            MCP servers ({{ selectedPlugin.mcp_servers.length }})
          </h3>
          <div
            v-if="!selectedPlugin.mcp_servers.length"
            class="rounded-md border border-dashed p-3 text-sm text-muted-foreground"
          >
            No MCP servers.
          </div>
          <div
            v-for="server in selectedPlugin.mcp_servers"
            :key="server.id"
            class="rounded-md border border-border p-3"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-medium">{{ server.name }}</span
              ><Badge variant="outline">{{ transportLabel(server.transport) }}</Badge
              ><Badge v-if="server.auth_type === 'oauth'" variant="secondary">OAuth</Badge>
            </div>
            <p class="mt-1 break-all font-mono text-xs text-muted-foreground">
              {{ mcpEndpoint(server) }}
            </p>
          </div>
        </section>

        <section aria-label="Credential services" class="space-y-2">
          <h3 class="text-sm font-semibold">
            Credential services ({{ selectedPlugin.credential_requirements.length }})
          </h3>
          <div
            v-if="!selectedPlugin.credential_requirements.length"
            class="rounded-md border border-dashed p-3 text-sm text-muted-foreground"
          >
            No credential services required.
          </div>
          <div
            v-for="requirement in selectedPlugin.credential_requirements"
            :key="requirement.id"
            class="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border p-3"
          >
            <div class="min-w-0 space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-medium">{{ requirement.service_name }}</span
                ><Badge variant="outline">{{ credentialTypeLabel(requirement.credential_type) }}</Badge
                ><Badge :variant="requirement.required ? 'secondary' : 'outline'">{{
                  requirement.required ? 'Required' : 'Optional'
                }}</Badge>
              </div>
              <p v-if="requirement.description" class="text-sm text-muted-foreground">
                {{ requirement.description }}
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              :data-testid="`plugin-add-credential-${requirement.service_id}`"
              @click="addCredential(requirement.service_id)"
              >Add Credential</Button
            >
          </div>
        </section>
      </div>
      <div
        v-else-if="detailUnavailable"
        class="space-y-3 rounded-md border border-destructive/30 bg-destructive/5 p-4"
        data-testid="plugin-detail-unavailable"
      >
        <p class="text-sm text-destructive">
          This plugin is unavailable in the current organization or no longer exists.
        </p>
        <Button variant="outline" size="sm" data-testid="plugin-back" @click="backToList"
          ><ArrowLeft /> Back to plugins</Button
        >
      </div>
      <div
        v-else-if="!pluginStore.plugins.length"
        class="overflow-hidden rounded-lg border border-border bg-card"
      >
        <EmptyState
          :icon="Puzzle"
          title="No plugins yet"
          description="Plugins available to this organization will appear here."
        />
      </div>
      <div v-else class="grid gap-2 sm:grid-cols-2" data-testid="plugin-catalog">
        <button
          v-for="plugin in pluginStore.plugins"
          :key="plugin.id"
          type="button"
          class="group flex min-h-28 flex-col items-start gap-2 rounded-lg border border-border bg-card p-4 text-left transition-colors hover:border-primary/40 hover:bg-muted/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :data-testid="`plugin-open-${plugin.id}`"
          @click="openPlugin(plugin)"
        >
          <div class="flex w-full items-center justify-between gap-2">
            <span class="flex min-w-0 items-center gap-2 text-sm font-semibold"
              ><Puzzle :size="16" class="shrink-0 text-muted-foreground" /><span class="truncate">{{
                plugin.name
              }}</span></span
            ><Badge variant="outline">{{ plugin.is_global ? 'Global' : 'Organization' }}</Badge>
          </div>
          <p class="line-clamp-2 text-sm text-muted-foreground">
            {{ plugin.description || 'No description provided.' }}
          </p>
          <div class="mt-auto flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <Badge :variant="plugin.org_enabled ? 'secondary' : 'outline'">{{ availabilityLabel(plugin) }}</Badge>
            <span>{{ plugin.skills.length }} {{ plugin.skills.length === 1 ? 'skill' : 'skills' }} ·</span>
            <span>{{ plugin.mcp_servers.length }} {{ plugin.mcp_servers.length === 1 ? 'MCP server' : 'MCP servers' }} ·</span>
            <span>{{ plugin.credential_requirements.length }} {{ plugin.credential_requirements.length === 1 ? 'service' : 'services' }}</span>
          </div>
        </button>
      </div>
    </SettingsSection>

    <PluginEditorDialog v-model:open="editorOpen" :plugin="editingPlugin" />
    <Dialog :open="!!deletingPlugin" @update:open="(value) => !value && (deletingPlugin = null)">
      <DialogContent
        ><DialogHeader
          ><DialogTitle>Delete Plugin</DialogTitle
          ><DialogDescription
            >Delete {{ deletingPlugin?.name }}? Its skills, MCP servers, and requirements will be
            removed. This cannot be undone.</DialogDescription
          ></DialogHeader
        ><DialogFooter
          ><Button variant="outline" :disabled="deleting" @click="deletingPlugin = null"
            >Cancel</Button
          ><Button
            variant="destructive"
            data-testid="plugin-delete-confirm"
            :disabled="deleting"
            @click="handleDelete"
            >{{ deleting ? 'Deleting…' : 'Delete' }}</Button
          ></DialogFooter
        ></DialogContent
      >
    </Dialog>
  </div>
</template>
