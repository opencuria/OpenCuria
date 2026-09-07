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
import { computed, onMounted, onUnmounted, watch } from 'vue'
import {
  BookText,
  Bot,
  Building2,
  Camera,
  Key,
  KeyRound,
  Layers,
  Server,
  Settings2,
  Shield,
} from '@lucide/vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { useAuthStore } from '@/stores/auth'
import { cn } from '@/lib/utils'
import WorkspacePolicyTab from './WorkspacePolicyTab.vue'
import ProviderConfigTab from './ProviderConfigTab.vue'
import CredentialServicesTab from './CredentialServicesTab.vue'
import SkillsPanel from './SkillsPanel.vue'
import CredentialsPanel from './CredentialsPanel.vue'
import ApiKeysPanel from './ApiKeysPanel.vue'
import CapturedImagesPanel from './CapturedImagesPanel.vue'
import RunnersPanel from './RunnersPanel.vue'
import ImageDefinitionsTab from '@/components/images/ImageDefinitionsTab.vue'
import {
  OPEN_SETTINGS_EVENT,
  resolveSettingsTab,
  type SettingsTabId,
} from './settingsTabs'

const authStore = useAuthStore()

const open = defineModel<boolean>('open', { default: false })
const activeTab = defineModel<SettingsTabId>('tab', { default: 'general' })

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
  { id: 'skills', label: 'Skills', icon: BookText },
  { id: 'credentials', label: 'Credentials', icon: KeyRound },
  { id: 'api-keys', label: 'API Keys', icon: Key },
  { id: 'images', label: 'Captured Images', icon: Camera },
  { id: 'runners', label: 'Runners', icon: Server, adminOnly: true },
  { id: 'credential-services', label: 'Credential Services', icon: Shield },
  { id: 'image-definitions', label: 'Image Definitions', icon: Layers },
]

const visibleNavItems = computed(() =>
  navItems.filter((item) => !item.adminOnly || isAdmin.value),
)

const activeLabel = computed(
  () => visibleNavItems.value.find((item) => item.id === activeTab.value)?.label ?? 'Settings',
)

function openSheet(tab?: unknown): void {
  const next = resolveSettingsTab(tab)
  // Runners is admin-only — fall back to General.
  activeTab.value = next === 'runners' && !isAdmin.value ? 'general' : next
  open.value = true
}

function selectTab(id: SettingsTabId): void {
  activeTab.value = id
}

function handleSettingsEvent(event: Event): void {
  const detail = (event as CustomEvent<{ tab?: unknown }>).detail
  openSheet(detail?.tab)
}

onMounted(() => {
  window.addEventListener(OPEN_SETTINGS_EVENT, handleSettingsEvent)
})

onUnmounted(() => {
  window.removeEventListener(OPEN_SETTINGS_EVENT, handleSettingsEvent)
})

// If an admin has Runners open and loses the role (org switch), fall back.
watch(isAdmin, (admin) => {
  if (!admin && activeTab.value === 'runners') {
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
      class="max-w-[80rem]! sm:max-w-[80rem]! w-[calc(100vw-2rem)]! h-[min(54rem,80dvh)] max-h-[calc(100dvh-2rem)] rounded-2xl p-0 gap-0 flex flex-col md:flex-row overflow-hidden"
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
        class="shrink-0 border-b border-border md:w-60 md:border-b-0 md:border-r"
      >
        <!-- Mobile: horizontal chips -->
        <div
          class="flex gap-1.5 overflow-x-auto p-2 md:hidden"
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
        <div class="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3 lg:px-6">
          <h2 class="text-base font-semibold text-foreground" data-testid="settings-sheet-title">
            {{ activeLabel }}
          </h2>
        </div>
        <ScrollArea class="min-h-0 flex-1">
          <div class="mx-auto w-full max-w-3xl p-4 lg:p-6" role="tabpanel" :aria-label="activeLabel">
            <WorkspacePolicyTab v-if="activeTab === 'general'" />
            <ProviderConfigTab v-else-if="activeTab === 'provider'" />
            <SkillsPanel v-else-if="activeTab === 'skills'" />
            <CredentialsPanel v-else-if="activeTab === 'credentials'" />
            <ApiKeysPanel v-else-if="activeTab === 'api-keys'" />
            <CapturedImagesPanel v-else-if="activeTab === 'images'" />
            <RunnersPanel v-else-if="activeTab === 'runners' && isAdmin" />
            <CredentialServicesTab v-else-if="activeTab === 'credential-services'" />
            <ImageDefinitionsTab v-else-if="activeTab === 'image-definitions'" />
          </div>
        </ScrollArea>
      </div>
    </DialogContent>
  </Dialog>
</template>
