<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  BookText,
  ChevronRight,
  Globe,
  KeyRound,
  Lock,
  Pencil,
  Plus,
  Puzzle,
  Terminal,
  Trash2,
} from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import CopyButton from '@/components/common/CopyButton.vue'
import DetailHeader from '@/components/common/DetailHeader.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import LoadingSpinner from '@/components/common/LoadingSpinner.vue'
import SettingsSection from './SettingsSection.vue'
import PluginEditorDialog from './PluginEditorDialog.vue'
import { usePluginStore } from '@/stores/plugins'
import { useAuthStore } from '@/stores/auth'
import {
  PLUGIN_SCOPES,
  canTogglePlugin,
  credentialSourceLabel,
  mcpEndpoint,
  mcpTransportLabel,
  pluginAvailability,
  pluginContents,
  pluginScopeLabel,
  pluginsOfScope,
  serverVariableList,
  type Plugin,
  type PluginScope,
} from '@opencuria/chat-core/plugins'

const props = defineProps<{ initialPluginId?: string; contextVersion?: number }>()
const emit = defineEmits<{ 'detail-change': [open: boolean] }>()
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
const detailOpen = computed(() => Boolean(selectedPlugin.value) && !pluginStore.error)
watch(detailOpen, (open) => emit('detail-change', open), { immediate: true })
onUnmounted(() => emit('detail-change', false))
const editorOpen = ref(false)
const editingPlugin = ref<Plugin | null>(null)
const deletingPlugin = ref<Plugin | null>(null)
const deleting = ref(false)
const expandedSkills = ref<string[]>([])

const SECTION_TITLES: Record<PluginScope, string> = {
  personal: 'Your plugins',
  organization: 'Organization',
  builtin: 'Built-in',
}
const sections = computed(() =>
  PLUGIN_SCOPES.map((scope) => ({
    scope,
    title: SECTION_TITLES[scope],
    plugins: pluginsOfScope(pluginStore.plugins, scope),
  })).filter((section) => section.scope === 'personal' || section.plugins.length),
)

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
function onSaved(plugin: Plugin): void {
  if (!editingPlugin.value) openPlugin(plugin)
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
    backToList()
    deletingPlugin.value = null
  }
}
function setSkillOpen(id: string, value: boolean): void {
  expandedSkills.value = value
    ? [...expandedSkills.value, id]
    : expandedSkills.value.filter((entry) => entry !== id)
}
/** "Not released" or "Unpublished" for admins; nothing when usable. */
function availabilityNote(plugin: Plugin): string {
  if (!isAdmin.value || plugin.scope === 'personal') return ''
  const status = pluginAvailability(plugin)
  return status === 'Available' ? '' : status
}
</script>

<template>
  <div class="min-w-0 space-y-6">
    <div v-if="detailOpen && selectedPlugin" class="min-w-0 space-y-6" data-testid="plugin-detail">
      <DetailHeader
        :title="selectedPlugin.name"
        back-label="Plugins"
        :icon="Puzzle"
        :description="selectedPlugin.description"
        @back="backToList"
      >
        <template #meta>
          <span>{{ pluginScopeLabel(selectedPlugin.scope) }}</span>
          <span v-if="pluginContents(selectedPlugin)" aria-hidden="true">·</span>
          <span v-if="pluginContents(selectedPlugin)">{{ pluginContents(selectedPlugin) }}</span>
        </template>
        <template #actions>
          <label
            v-if="isAdmin && selectedPlugin.scope !== 'personal'"
            class="flex items-center gap-2 rounded-full border border-border px-3 py-1 text-xs"
          >
            <span>{{
              selectedPlugin.org_enabled ? 'Available to members' : 'Not released'
            }}</span>
            <Switch
              :model-value="selectedPlugin.org_enabled"
              :disabled="
                !canTogglePlugin(selectedPlugin) ||
                pluginStore.togglingIds.includes(selectedPlugin.id)
              "
              :aria-label="
                selectedPlugin.org_enabled
                  ? `Withdraw ${selectedPlugin.name}`
                  : `Release ${selectedPlugin.name}`
              "
              :data-testid="`plugin-toggle-${selectedPlugin.id}`"
              @update:model-value="(value) => handleToggle(selectedPlugin!, Boolean(value))"
            />
          </label>
          <template v-if="selectedPlugin.editable">
            <Button
              variant="outline"
              size="sm"
              data-testid="plugin-edit-detail"
              @click="openEdit(selectedPlugin!)"
              ><Pencil /> Edit</Button
            >
            <Button
              variant="ghost"
              size="sm"
              class="text-muted-foreground hover:text-destructive"
              data-testid="plugin-delete-detail"
              @click="deletingPlugin = selectedPlugin"
              ><Trash2 /> Delete</Button
            >
          </template>
        </template>
      </DetailHeader>

      <p
        v-if="!selectedPlugin.skills.length && !selectedPlugin.mcp_servers.length"
        class="rounded-lg border border-dashed border-border px-4 py-3 text-sm text-muted-foreground"
      >
        This plugin is empty.{{
          selectedPlugin.editable ? ' Edit it to add skills or MCP servers.' : ''
        }}
      </p>

      <SettingsSection v-if="selectedPlugin.skills.length" title="Skills" aria-label="Skills">
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <Collapsible
            v-for="skill in [...selectedPlugin.skills].sort((a, b) => a.position - b.position)"
            :key="skill.id"
            :open="expandedSkills.includes(skill.id)"
            @update:open="setSkillOpen(skill.id, $event)"
          >
            <CollapsibleTrigger
              class="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            >
              <BookText :size="16" class="mt-0.5 shrink-0 text-muted-foreground" />
              <span class="min-w-0 flex-1">
                <span class="block text-sm font-medium">{{ skill.name }}</span>
                <span
                  v-if="skill.description"
                  class="mt-0.5 block text-sm text-muted-foreground"
                  :class="expandedSkills.includes(skill.id) ? undefined : 'line-clamp-1'"
                  >{{ skill.description }}</span
                >
              </span>
              <ChevronRight
                :size="15"
                class="mt-0.5 shrink-0 text-muted-foreground transition-transform"
                :class="expandedSkills.includes(skill.id) ? 'rotate-90' : undefined"
              />
            </CollapsibleTrigger>
            <CollapsibleContent>
              <pre
                class="mx-4 mb-4 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/50 px-3 py-2.5 font-mono text-xs leading-relaxed text-foreground/90"
                >{{ skill.body }}</pre
              >
            </CollapsibleContent>
          </Collapsible>
        </div>
      </SettingsSection>

      <SettingsSection
        v-if="selectedPlugin.mcp_servers.length"
        title="MCP servers"
        aria-label="MCP servers"
      >
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div v-for="server in selectedPlugin.mcp_servers" :key="server.id" class="px-4 py-3">
            <div class="flex items-center gap-3">
              <component
                :is="server.transport === 'stdio' ? Terminal : Globe"
                :size="16"
                class="shrink-0 text-muted-foreground"
              />
              <span class="min-w-0 flex-1 truncate text-sm font-medium">{{ server.name }}</span>
              <Badge v-if="server.auth_type === 'oauth'" variant="secondary">OAuth</Badge>
              <Badge variant="outline">{{ mcpTransportLabel(server.transport) }}</Badge>
            </div>
            <div
              v-if="mcpEndpoint(server)"
              class="mt-2 flex items-center gap-1 rounded-md bg-muted/50 pl-3 pr-1 ml-7"
            >
              <code class="min-w-0 flex-1 truncate py-1.5 font-mono text-xs">{{
                mcpEndpoint(server)
              }}</code>
              <CopyButton :text="mcpEndpoint(server)" label="Copy" />
            </div>
            <div
              v-if="serverVariableList(server).length"
              class="mt-2 ml-7 flex flex-wrap gap-1.5"
            >
              <Badge
                v-for="variable in serverVariableList(server)"
                :key="variable.name"
                variant="outline"
                class="font-mono"
                :title="variable.secret ? `${variable.name} comes from a credential` : undefined"
                ><Lock v-if="variable.secret" />{{ variable.name }}</Badge
              >
            </div>
          </div>
        </div>
      </SettingsSection>

      <SettingsSection
        v-if="selectedPlugin.credential_requirements.length"
        title="Credentials"
        aria-label="Credentials"
      >
        <div class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card">
          <div
            v-for="requirement in selectedPlugin.credential_requirements"
            :key="requirement.id"
            class="flex flex-wrap items-center gap-3 px-4 py-3"
          >
            <KeyRound :size="16" class="shrink-0 text-muted-foreground" />
            <div class="min-w-0 flex-1">
              <p class="text-sm font-medium">
                {{ requirement.service_name }}
                <span v-if="!requirement.required" class="text-xs font-normal text-muted-foreground"
                  >· optional</span
                >
              </p>
              <p class="text-xs text-muted-foreground" :data-testid="`plugin-credential-source-${requirement.key}`">
                {{ credentialSourceLabel(requirement)
                }}<template v-if="requirement.description">
                  · {{ requirement.description }}</template
                >
              </p>
            </div>
            <Button
              v-if="!requirement.credential_id"
              size="sm"
              variant="outline"
              :data-testid="`plugin-add-credential-${requirement.service_id}`"
              @click="addCredential(requirement.service_id)"
              >Add credential</Button
            >
          </div>
        </div>
      </SettingsSection>
    </div>

    <SettingsSection
      v-else
      description="Plugins give your agents skills and tools (MCP servers). Enable them per workspace."
    >
      <template #actions>
        <Button size="sm" data-testid="plugin-create" @click="createPlugin"
          ><Plus /> New plugin</Button
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
      <div v-else class="space-y-6" data-testid="plugin-catalog">
        <section v-for="section in sections" :key="section.scope" class="space-y-2">
          <h4 class="text-xs font-medium text-muted-foreground">{{ section.title }}</h4>
          <div
            v-if="section.plugins.length"
            class="divide-y divide-border overflow-hidden rounded-lg border border-border bg-card"
          >
            <button
              v-for="plugin in section.plugins"
              :key="plugin.id"
              type="button"
              class="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              :data-testid="`plugin-open-${plugin.id}`"
              @click="openPlugin(plugin)"
            >
              <span
                class="flex size-9 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground"
                ><Puzzle :size="17"
              /></span>
              <span class="min-w-0 flex-1">
                <span class="flex items-center gap-2">
                  <span class="truncate text-sm font-medium">{{ plugin.name }}</span>
                  <Badge v-if="availabilityNote(plugin)" variant="outline">{{
                    availabilityNote(plugin)
                  }}</Badge>
                </span>
                <span
                  v-if="plugin.description"
                  class="mt-0.5 block truncate text-xs text-muted-foreground"
                  >{{ plugin.description }}</span
                >
              </span>
              <span class="hidden shrink-0 text-xs text-muted-foreground sm:block">{{
                pluginContents(plugin) || 'Empty'
              }}</span>
              <ChevronRight :size="15" class="shrink-0 text-muted-foreground" />
            </button>
          </div>
          <div
            v-else
            class="overflow-hidden rounded-lg border border-dashed border-border"
          >
            <EmptyState
              :icon="Puzzle"
              title="No plugins of your own yet"
              description="Create one, or ask an agent to build a plugin for you."
            >
              <template #action>
                <Button size="sm" variant="outline" data-testid="plugin-create-empty" @click="createPlugin"
                  ><Plus /> New plugin</Button
                >
              </template>
            </EmptyState>
          </div>
        </section>
      </div>
    </SettingsSection>

    <PluginEditorDialog v-model:open="editorOpen" :plugin="editingPlugin" @saved="onSaved" />
    <Dialog :open="!!deletingPlugin" @update:open="(value) => !value && (deletingPlugin = null)">
      <DialogContent
        ><DialogHeader
          ><DialogTitle>Delete plugin</DialogTitle
          ><DialogDescription
            >Delete {{ deletingPlugin?.name }}? Workspaces lose its skills and MCP servers. This
            cannot be undone.</DialogDescription
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
