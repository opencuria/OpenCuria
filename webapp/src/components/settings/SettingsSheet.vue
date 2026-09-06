<!--
  SettingsSheet — Großes Settings-Modal mit Seiten-Nav (Schritt 5).

  Layout-Pattern angelehnt an OpenWebUI (chat/SettingsModal.svelte +
  common/Modal.svelte, size=full): links Nav (w-60 border-r), rechts Content.
  Inhalte sind OpenCuria-eigene Panels (keine OpenWebUI-Tabs).

  Öffnen:  (a) global via Window-Event `opencuria:open-settings`
             (CustomEvent, detail `{ tab?: string }`),
           (b) kontrolliert via v-model:open (defineModel).
  Tab-Auswahl: `resolveSettingsTab()` — mappt auch alte OrgSettings-Tabs
  (`workspace-policies`, `provider`, `image-definitions`,
  `credential-services`) und Legacy-Tabs (`preferences`, `theme` …).
  Runners-Tab ist nur für Admins sichtbar (authStore.isAdmin).
  Focus-Trap/Esc/Backdrop kommen vom Dialog (reka-ui) gratis.
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
  Server,
  Settings2,
  Users,
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
  { id: 'general', label: 'Allgemein', icon: Settings2 },
  { id: 'provider', label: 'Provider & Modelle', icon: Bot },
  { id: 'skills', label: 'Skills', icon: BookText },
  { id: 'credentials', label: 'Credentials', icon: KeyRound },
  { id: 'api-keys', label: 'API Keys', icon: Key },
  { id: 'images', label: 'Captured Images', icon: Camera },
  { id: 'runners', label: 'Runners', icon: Server, adminOnly: true },
  { id: 'organization', label: 'Organization', icon: Users },
]

const visibleNavItems = computed(() =>
  navItems.filter((item) => !item.adminOnly || isAdmin.value),
)

const activeLabel = computed(
  () => visibleNavItems.value.find((item) => item.id === activeTab.value)?.label ?? 'Einstellungen',
)

function openSheet(tab?: unknown): void {
  const next = resolveSettingsTab(tab)
  // Runners nur für Admins — sonst auf Allgemein zurückfallen.
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

// Falls ein Admin den Runners-Tab offen hat und die Rolle verliert (Org-Wechsel),
// zurück auf Allgemein wechseln.
watch(isAdmin, (admin) => {
  if (!admin && activeTab.value === 'runners') {
    activeTab.value = 'general'
  }
})
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent
      aria-describedby="settings-sheet-description"
      class="max-w-[80rem] w-[calc(100vw-2rem)] h-[min(54rem,80dvh)] max-h-[calc(100dvh-2rem)] rounded-2xl p-0 gap-0 flex flex-col md:flex-row overflow-hidden"
      data-testid="settings-sheet"
      @open-auto-focus.prevent
    >
      <DialogTitle class="sr-only">Einstellungen</DialogTitle>
      <DialogDescription id="settings-sheet-description" class="sr-only">
        Organisations-, Provider- und Harness-Einstellungen.
      </DialogDescription>

      <!-- Seiten-Nav: mobil horizontal, ab md vertikal -->
      <nav
        aria-label="Einstellungen"
        class="shrink-0 border-b border-border md:w-60 md:border-b-0 md:border-r"
      >
        <!-- Mobil: horizontale Chips -->
        <div
          class="flex gap-1.5 overflow-x-auto p-2 md:hidden"
          role="tablist"
          aria-label="Einstellungen-Tabs"
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

        <!-- Desktop: vertikale Liste -->
        <ScrollArea class="hidden h-full md:block">
          <div class="flex flex-col gap-0.5 p-2" role="tablist" aria-label="Einstellungen-Tabs">
            <div class="flex items-center gap-2 px-2.5 pb-2 pt-1.5">
              <Building2 :size="16" class="text-muted-foreground" aria-hidden="true" />
              <span class="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                Einstellungen
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
          <div class="p-4 lg:p-6" role="tabpanel" :aria-label="activeLabel">
            <WorkspacePolicyTab v-if="activeTab === 'general'" />
            <ProviderConfigTab v-else-if="activeTab === 'provider'" />
            <SkillsPanel v-else-if="activeTab === 'skills'" />
            <CredentialsPanel v-else-if="activeTab === 'credentials'" />
            <ApiKeysPanel v-else-if="activeTab === 'api-keys'" />
            <CapturedImagesPanel v-else-if="activeTab === 'images'" />
            <RunnersPanel v-else-if="activeTab === 'runners' && isAdmin" />
            <div v-else-if="activeTab === 'organization'" class="space-y-8">
              <section aria-label="Credential Services">
                <h3 class="mb-3 text-sm font-semibold text-foreground">Credential Services</h3>
                <CredentialServicesTab />
              </section>
              <section aria-label="Image Definitions">
                <h3 class="mb-3 text-sm font-semibold text-foreground">Image Definitions</h3>
                <ImageDefinitionsTab />
              </section>
            </div>
          </div>
        </ScrollArea>
      </div>
    </DialogContent>
  </Dialog>
</template>
