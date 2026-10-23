<!--
  SettingsSheet — large settings modal with side navigation.

  Layout: left nav (w-60 border-r), right content. Contents are
  OpenCuria-owned panels.

  Open:  (a) global via Window event `opencuria:open-settings`
             (CustomEvent, detail `{ tab?: string }`),
           (b) controlled via v-model:open (defineModel).
  Tab selection: `resolveSettingsTab()` — also maps old OrgSettings tabs.
  Runners tab is admin-only (authStore.isAdmin).
  Focus trap / Esc / backdrop come from Dialog (reka-ui).
-->
<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  BookText,
  Bot,
  Building2,
  Camera,
  Key,
  KeyRound,
  Layers,
  Puzzle,
  Server,
  Settings2,
  Shield,
  Sparkles,
} from '@lucide/vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { useAuthStore } from '@/stores/auth'
import { cn } from '@/lib/utils'
import {
  CLOSE_SETTINGS_EVENT,
  OPEN_SETTINGS_EVENT,
  resolveSettingsTab,
  type SettingsTabId,
} from './settingsTabs'

const WorkspacePolicyTab = defineAsyncComponent(() => import('./WorkspacePolicyTab.vue'))
const ProviderConfigTab = defineAsyncComponent(() => import('./ProviderConfigTab.vue'))
const AgentConfigTab = defineAsyncComponent(() => import('./AgentConfigTab.vue'))
const CredentialServicesTab = defineAsyncComponent(() => import('./CredentialServicesTab.vue'))
const SkillsPanel = defineAsyncComponent(() => import('./SkillsPanel.vue'))
const CredentialsPanel = defineAsyncComponent(() => import('./CredentialsPanel.vue'))
const PluginsPanel = defineAsyncComponent(() => import('./PluginsPanel.vue'))
const ApiKeysPanel = defineAsyncComponent(() => import('./ApiKeysPanel.vue'))
const CapturedImagesPanel = defineAsyncComponent(() => import('./CapturedImagesPanel.vue'))
const RunnersPanel = defineAsyncComponent(() => import('./RunnersPanel.vue'))
const ImageDefinitionsTab = defineAsyncComponent(
  () => import('@/components/images/ImageDefinitionsTab.vue'),
)

const authStore = useAuthStore()

const open = defineModel<boolean>('open', { default: false })
const activeTab = defineModel<SettingsTabId>('tab', { default: 'general' })
const settingsContext = ref<{
  pluginId?: string
  serviceId?: string
  credentialId?: string
  workspaceDraftId?: string
  version: number
}>({ version: 0 })

const isAdmin = computed(() => authStore.isAdmin)

interface SettingsNavItem {
  id: SettingsTabId
  label: string
  icon: typeof Settings2
  adminOnly?: boolean
}

const navItems: SettingsNavItem[] = [
  { id: 'general', label: 'General', icon: Settings2 },
  { id: 'provider', label: 'Provider & Models', icon: Bot },
  { id: 'agents', label: 'Agents', icon: Sparkles },
  { id: 'skills', label: 'Skills', icon: BookText },
  { id: 'credentials', label: 'Credentials', icon: KeyRound },
  { id: 'plugins', label: 'Plugins', icon: Puzzle },
  { id: 'api-keys', label: 'API Keys', icon: Key },
  { id: 'images', label: 'Captured Images', icon: Camera },
  { id: 'runners', label: 'Runners', icon: Server, adminOnly: true },
  { id: 'credential-services', label: 'Credential Services', icon: Shield, adminOnly: true },
  { id: 'image-definitions', label: 'Image Definitions', icon: Layers },
]

const visibleNavItems = computed(() => navItems.filter((item) => !item.adminOnly || isAdmin.value))

const activeLabel = computed(
  () => visibleNavItems.value.find((item) => item.id === activeTab.value)?.label ?? 'Settings',
)

function openSheet(tab?: unknown): void {
  const next = resolveSettingsTab(tab)
  // Runners is admin-only — fall back to General.
  activeTab.value =
    (next === 'runners' || next === 'credential-services') && !isAdmin.value ? 'general' : next
  open.value = true
}

function selectTab(id: SettingsTabId): void {
  activeTab.value = id
}

function clearCredentialsContext(kind: 'service' | 'credential' | 'finished'): void {
  const version = settingsContext.value.version
  if (kind === 'service') {
    settingsContext.value = { version, workspaceDraftId: settingsContext.value.workspaceDraftId }
  } else if (kind === 'credential') {
    settingsContext.value = { version, workspaceDraftId: settingsContext.value.workspaceDraftId }
  } else {
    settingsContext.value = { version }
  }
}

const selectedRunnerId = ref<string | undefined>()
const selectedRunnerActivity = ref(false)
// Tab whose panel currently shows a detail view; detail views use the full width.
const detailTab = ref<SettingsTabId | null>(null)

function setDetailOpen(tab: SettingsTabId, isOpen: boolean): void {
  // A previous panel may report its unmount after the next panel opened a detail.
  if (isOpen) detailTab.value = tab
  else if (detailTab.value === tab) detailTab.value = null
}

watch(activeTab, (tab, previous) => {
  detailTab.value = null
  if (previous === 'runners' && tab !== 'runners') {
    selectedRunnerId.value = undefined
    selectedRunnerActivity.value = false
  }
})
watch(open, (isOpen) => {
  if (!isOpen) {
    selectedRunnerId.value = undefined
    selectedRunnerActivity.value = false
    detailTab.value = null
    settingsContext.value = { version: settingsContext.value.version + 1 }
  }
})

function handleSettingsEvent(event: Event): void {
  const detail = (
    event as CustomEvent<{
      tab?: unknown
      pluginId?: string
      serviceId?: string
      credentialId?: string
      runnerId?: string
      runnerActivity?: boolean
      workspaceDraftId?: string
    }>
  ).detail
  settingsContext.value = {
    pluginId: detail?.pluginId,
    serviceId: detail?.serviceId,
    credentialId: detail?.credentialId,
    workspaceDraftId: detail?.workspaceDraftId,
    version: settingsContext.value.version + 1,
  }
  selectedRunnerId.value = detail?.runnerId
  selectedRunnerActivity.value = detail?.runnerActivity === true
  openSheet(detail?.tab)
}

function closeSheet(): void {
  open.value = false
}
onMounted(() => {
  window.addEventListener(OPEN_SETTINGS_EVENT, handleSettingsEvent)
  window.addEventListener(CLOSE_SETTINGS_EVENT, closeSheet)
})

onUnmounted(() => {
  window.removeEventListener(OPEN_SETTINGS_EVENT, handleSettingsEvent)
  window.removeEventListener(CLOSE_SETTINGS_EVENT, closeSheet)
})

// If an admin has Runners open and loses the role (org switch), fall back.
watch(isAdmin, (admin) => {
  if (!admin && (activeTab.value === 'runners' || activeTab.value === 'credential-services')) {
    activeTab.value = 'general'
  }
})
</script>

<template>
  <Dialog v-model:open="open">
    <!-- Width uses the Tailwind v4 important modifier (trailing `!`):
      tailwind-merge already replaces Dialog's `sm:max-w-md` / `w-full`,
      and `!` additionally guards stylesheet order if the class is ever
      concatenated without merge. -->
    <DialogContent
      aria-describedby="settings-sheet-description"
      class="max-w-[80rem]! sm:max-w-[80rem]! w-[calc(100vw-2rem)]! h-[min(54rem,80dvh)] max-h-[calc(100dvh-2rem)] rounded-2xl min-w-0 p-0 gap-0 flex flex-col md:flex-row overflow-hidden"
      data-testid="settings-sheet"
      @open-auto-focus.prevent
    >
      <DialogTitle class="sr-only">Settings</DialogTitle>
      <DialogDescription id="settings-sheet-description" class="sr-only">
        Organization, provider, and harness settings.
      </DialogDescription>

      <!-- Side nav: horizontal on mobile, vertical from md -->
      <nav
        aria-label="Settings"
        class="min-w-0 max-w-full shrink-0 border-b border-border md:w-60 md:border-b-0 md:border-r"
      >
        <!-- Mobile: horizontal chips -->
        <div
          class="flex min-w-0 max-w-full gap-1.5 overflow-x-auto p-2 md:hidden"
          role="tablist"
          aria-label="Settings tabs"
        >
          <button
            v-for="item in visibleNavItems"
            :key="item.id"
            type="button"
            role="tab"
            :aria-selected="activeTab === item.id"
            :data-testid="`settings-nav-${item.id}`"
            :class="
              cn(
                'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors',
                activeTab === item.id
                  ? 'border-primary/40 bg-primary/10 text-primary'
                  : 'border-border text-muted-foreground hover:text-foreground',
              )
            "
            @click="selectTab(item.id)"
          >
            <component :is="item.icon" :size="14" aria-hidden="true" />
            {{ item.label }}
          </button>
        </div>

        <!-- Desktop: vertical list -->
        <ScrollArea class="hidden h-full md:block">
          <div class="flex flex-col gap-0.5 p-2" role="tablist" aria-label="Settings tabs">
            <div class="flex items-center gap-2 px-2.5 pb-2 pt-1.5">
              <Building2 :size="16" class="text-muted-foreground" aria-hidden="true" />
              <span class="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Settings
              </span>
            </div>
            <button
              v-for="item in visibleNavItems"
              :key="item.id"
              type="button"
              role="tab"
              :aria-selected="activeTab === item.id"
              :data-testid="`settings-nav-${item.id}`"
              :class="
                cn(
                  'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium transition-colors',
                  activeTab === item.id
                    ? 'bg-muted text-foreground'
                    : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground',
                )
              "
              @click="selectTab(item.id)"
            >
              <component :is="item.icon" :size="16" aria-hidden="true" />
              {{ item.label }}
            </button>
          </div>
        </ScrollArea>
      </nav>

      <!-- Content -->
      <div class="flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          class="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3 lg:px-6"
        >
          <h2 class="text-base font-semibold text-foreground" data-testid="settings-sheet-title">
            {{ activeLabel }}
          </h2>
        </div>
        <ScrollArea class="min-h-0 min-w-0 flex-1">
          <div
            class="mx-auto min-w-0 w-full p-4 lg:p-6"
            :class="detailTab === activeTab ? 'max-w-none' : 'max-w-3xl'"
            role="tabpanel"
            :aria-label="activeLabel"
          >
            <WorkspacePolicyTab v-if="open && activeTab === 'general'" />
            <ProviderConfigTab v-else-if="open && activeTab === 'provider'" />
            <AgentConfigTab v-else-if="open && activeTab === 'agents'" />
            <SkillsPanel v-else-if="open && activeTab === 'skills'" />
            <CredentialsPanel
              v-else-if="open && activeTab === 'credentials'"
              :initial-service-id="settingsContext.serviceId"
              :initial-credential-id="settingsContext.credentialId"
              :workspace-draft-id="settingsContext.workspaceDraftId"
              :context-version="settingsContext.version"
              @context-consumed="clearCredentialsContext"
            />
            <PluginsPanel
              v-else-if="open && activeTab === 'plugins'"
              :initial-plugin-id="settingsContext.pluginId"
              :context-version="settingsContext.version"
              @detail-change="setDetailOpen('plugins', $event)"
            />
            <ApiKeysPanel v-else-if="open && activeTab === 'api-keys'" />
            <CapturedImagesPanel v-else-if="open && activeTab === 'images'" />
            <RunnersPanel
              v-else-if="open && activeTab === 'runners' && isAdmin"
              :runner-id="selectedRunnerId"
              :runner-activity="selectedRunnerActivity"
              :context-version="settingsContext.version"
              @detail-change="setDetailOpen('runners', $event)"
            />
            <CredentialServicesTab v-else-if="open && activeTab === 'credential-services'" />
            <ImageDefinitionsTab v-else-if="open && activeTab === 'image-definitions'" />
          </div>
        </ScrollArea>
      </div>
    </DialogContent>
  </Dialog>
</template>
