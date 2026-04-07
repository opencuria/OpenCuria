<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, RouterLink, useRouter } from 'vue-router'
import {
  LayoutDashboard,
  Server,
  Container,
  KeyRound,
  BookText,
  BookOpen,
  Sun,
  Moon,
  Monitor,
  ChevronsUpDown,
  Check,
  LogOut,
  Plus,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  Wifi,
  WifiOff,
  Shield,
  X,
  Key,
  Camera,
  Settings2,
} from 'lucide-vue-next'
import { useTheme } from '@/composables/useTheme'
import { useAuthStore } from '@/stores/auth'
import { connect as connectSocket, disconnect as disconnectSocket } from '@/services/socket'
import { isConnected } from '@/services/socket'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'

defineProps<{
  mobileOpen?: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const route = useRoute()
const router = useRouter()
const { mode, setTheme } = useTheme()
const authStore = useAuthStore()

const orgDropdownOpen = ref(false)
const isCollapsed = ref(false)

const SECTION_KEY = 'opencuria:sidebar-sections'

function loadSectionState(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(SECTION_KEY) ?? '{}')
  } catch {
    return {}
  }
}

const sectionState = ref<Record<string, boolean>>(loadSectionState())

function isSectionOpen(id: string): boolean {
  return sectionState.value[id] !== false
}

function toggleSection(id: string): void {
  sectionState.value[id] = !isSectionOpen(id)
  localStorage.setItem(SECTION_KEY, JSON.stringify(sectionState.value))
}

const isAdmin = computed(() => authStore.isAdmin)

const navSections = computed(() => [
  {
    id: 'main',
    label: null,
    items: [
      { to: '/', label: 'Dashboard', icon: LayoutDashboard },
    ],
  },
  {
    id: 'workspaces',
    label: 'Workspaces',
    items: [
      { to: '/workspaces', label: 'Workspaces', icon: Container },
      { to: '/images', label: 'Captured Images', icon: Camera },
    ],
  },
  {
    id: 'configuration',
    label: 'Configuration',
    items: [
      { to: '/skills', label: 'Skills', icon: BookText },
      { to: '/credentials', label: 'Credentials', icon: KeyRound },
      { to: '/api-keys', label: 'API Keys', icon: Key },
    ],
  },
  ...(isAdmin.value
    ? [
        {
          id: 'admin',
          label: 'Admin',
          items: [
            { to: '/runners', label: 'Runners', icon: Server },
            { to: '/org-settings', label: 'Settings', icon: Settings2 },
          ],
        },
      ]
    : []),
  {
    id: 'resources',
    label: 'Resources',
    items: [
      { to: '/docs', label: 'Docs', icon: BookOpen },
    ],
  },
])

function isActive(path: string): boolean {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

const themeIcon = computed(() => {
  if (mode.value === 'light') return Sun
  if (mode.value === 'dark') return Moon
  return Monitor
})

const themeLabel = computed(() => {
  if (mode.value === 'light') return 'Light'
  if (mode.value === 'dark') return 'Dark'
  return 'System'
})

function cycleTheme(): void {
  const next = mode.value === 'light' ? 'dark' : mode.value === 'dark' ? 'auto' : 'light'
  setTheme(next)
}

function switchOrganization(orgId: string): void {
  authStore.setActiveOrganization(orgId)
  orgDropdownOpen.value = false
  disconnectSocket()
  connectSocket()
  router.go(0)
}

function handleLogout(): void {
  authStore.logout()
  disconnectSocket()
  router.push('/login')
}

const userInitials = computed(() => {
  const email = authStore.user?.email ?? ''
  return email.charAt(0).toUpperCase()
})
</script>

<template>
  <aside
    class="flex flex-col h-full transition-[width] duration-[350ms] shrink-0"
    :class="[
      isCollapsed
        ? 'w-[var(--sidebar-collapsed-width)] min-w-[var(--sidebar-collapsed-width)]'
        : 'w-[var(--sidebar-width)] min-w-[var(--sidebar-width)]',
      mobileOpen ? 'fixed inset-y-0 left-0 z-40 lg:relative lg:z-auto' : 'hidden lg:flex',
    ]"
    style="
      background: var(--glass-bg-strong);
      backdrop-filter: var(--glass-filter);
      -webkit-backdrop-filter: var(--glass-filter);
      border-right: 1px solid var(--sidebar-divider-color);
      box-shadow: var(--glass-shadow-sm);
      will-change: backdrop-filter;
      [transition-timing-function:var(--spring-gentle)];
    "
  >
    <!-- Brand -->
    <div class="flex items-center gap-3 px-4 py-4 relative shrink-0">
      <OpenCuriaLogo :icon-only="isCollapsed" alt="OpenCuria" :class="isCollapsed ? 'h-9 w-9' : 'h-10 w-auto'" />

      <!-- Mobile close -->
      <button
        v-if="mobileOpen"
        class="lg:hidden absolute right-3 top-1/2 -translate-y-1/2 w-6 h-6 rounded-full flex items-center justify-center text-muted-fg hover:text-fg cursor-pointer transition-[background,color] duration-[200ms]"
        style="
          background: var(--glass-bg-subtle);
          border: 1px solid var(--glass-border);
        "
        title="Close menu"
        @click="emit('close')"
      >
        <X :size="12" />
      </button>

      <!-- Desktop collapse toggle -->
      <button
        v-else
        class="hidden lg:flex absolute -right-3 top-1/2 -translate-y-1/2 w-6 h-6 rounded-full items-center justify-center text-muted-fg hover:text-fg z-10 cursor-pointer transition-[background,color,transform] duration-[200ms]"
        style="
          background: var(--glass-bg);
          backdrop-filter: var(--glass-filter-subtle);
          border: 1px solid var(--glass-border);
          box-shadow: var(--glass-shadow-sm);
          [transition-timing-function:var(--spring-snappy)];
        "
        @click="isCollapsed = !isCollapsed"
        :title="isCollapsed ? 'Expand sidebar' : 'Collapse sidebar'"
      >
        <component :is="isCollapsed ? ChevronRight : ChevronLeft" :size="11" />
      </button>
    </div>

    <!-- Organization Switcher -->
    <div class="px-3 mb-2 relative shrink-0" v-if="authStore.organizations.length > 0">
      <button
        class="flex items-center justify-between w-full px-2.5 py-2 text-xs text-fg cursor-pointer transition-[background,border-color] duration-[200ms]"
        style="
          border-radius: var(--radius-sm);
          background: var(--glass-bg-subtle);
          backdrop-filter: var(--glass-filter-subtle);
          border: 1px solid var(--sidebar-org-switcher-border);
        "
        @click="orgDropdownOpen = !orgDropdownOpen"
        :title="isCollapsed ? authStore.activeOrganization?.name : ''"
      >
        <div v-if="!isCollapsed" class="flex items-center gap-2 min-w-0">
          <!-- Org avatar -->
          <div
            class="w-4 h-4 rounded flex items-center justify-center text-primary text-[9px] font-bold shrink-0"
            style="background: oklch(0.55 0.20 258 / 0.15)"
          >
            {{ authStore.activeOrganization?.name?.charAt(0)?.toUpperCase() ?? '?' }}
          </div>
          <span class="truncate font-medium">
            {{ authStore.activeOrganization?.name ?? 'Select organization' }}
          </span>
        </div>
        <span v-else class="font-bold text-xs">
          {{ authStore.activeOrganization?.name?.charAt(0)?.toUpperCase() ?? '?' }}
        </span>
        <ChevronsUpDown v-if="!isCollapsed" :size="11" class="text-muted-fg shrink-0 ml-1" />
      </button>

      <!-- Backdrop -->
      <div v-if="orgDropdownOpen" class="fixed inset-0 z-40" @click="orgDropdownOpen = false" />

      <!-- Dropdown — glass panel -->
      <div
        v-if="orgDropdownOpen"
        class="absolute left-3 right-3 mt-1 z-50 py-1 max-h-48 overflow-y-auto"
        style="
          border-radius: var(--radius-md);
          background: var(--glass-bg-strong);
          backdrop-filter: var(--glass-filter-strong);
          -webkit-backdrop-filter: var(--glass-filter-strong);
          border: 1px solid var(--sidebar-org-switcher-border);
          box-shadow: var(--glass-shadow-lg);
        "
      >
        <button
          v-for="org in authStore.organizations"
          :key="org.id"
          class="flex items-center justify-between w-full px-3 py-2 text-sm text-fg cursor-pointer transition-[background] duration-[150ms] hover:bg-[var(--glass-bg-subtle)]"
          @click="switchOrganization(org.id)"
        >
          <span class="truncate">{{ org.name }}</span>
          <Check v-if="org.id === authStore.activeOrganizationId" :size="13" class="text-primary shrink-0 ml-2" />
        </button>
        <div style="border-top: 1px solid var(--sidebar-org-switcher-border); margin-top: 4px; padding-top: 4px;">
          <RouterLink
            to="/create-organization"
            class="flex items-center gap-2 w-full px-3 py-2 text-sm text-muted-fg hover:text-fg transition-[background,color] duration-[150ms] hover:bg-[var(--glass-bg-subtle)]"
            @click="orgDropdownOpen = false"
          >
            <Plus :size="13" />
            New organization
          </RouterLink>
        </div>
      </div>
    </div>

    <!-- Navigation -->
    <nav class="flex flex-col flex-1 px-2 overflow-y-auto overflow-x-hidden min-h-0">
      <template v-for="section in navSections" :key="section.id">
        <!-- Section divider -->
        <div
          v-if="section.id !== 'main'"
          class="mx-1 my-1"
          style="height: 1px; background: var(--sidebar-divider-color)"
        />

        <!-- Section header -->
        <button
          v-if="section.label && !isCollapsed"
          class="flex items-center justify-between w-full px-2 py-1.5 text-[10px] font-semibold uppercase tracking-widest text-muted-fg/60 hover:text-muted-fg transition-[color] duration-[150ms] rounded cursor-pointer select-none"
          @click="toggleSection(section.id)"
        >
          <span>{{ section.label }}</span>
          <ChevronDown
            :size="10"
            class="transition-transform duration-[200ms] shrink-0"
            :class="isSectionOpen(section.id) ? 'rotate-0' : '-rotate-90'"
          />
        </button>

        <!-- Nav items -->
        <template v-if="isCollapsed || !section.label || isSectionOpen(section.id)">
          <RouterLink
            v-for="item in section.items"
            :key="item.to"
            :to="item.to"
            class="flex items-center gap-2.5 text-sm font-medium my-0.5 transition-[background,color,box-shadow] duration-[200ms] cursor-pointer"
            :class="[
              isCollapsed ? 'px-2 py-2 justify-center' : 'px-2.5 py-2',
            ]"
            :style="[
              'border-radius: var(--radius-sm)',
              isActive(item.to)
                ? 'background: oklch(0.55 0.20 258 / 0.14); color: var(--color-primary); box-shadow: inset 0 1px 0 oklch(0.55 0.20 258 / 0.15)'
                : 'color: var(--color-muted-foreground)',
              '[transition-timing-function:var(--spring-snappy)]',
            ]"
            :title="isCollapsed ? item.label : ''"
            @click="mobileOpen && emit('close')"
          >
            <component :is="item.icon" :size="15" class="shrink-0" />
            <span v-if="!isCollapsed" style="letter-spacing: var(--tracking-body)">{{ item.label }}</span>
          </RouterLink>
        </template>
      </template>
    </nav>

    <!-- Bottom section -->
    <div
      class="shrink-0 px-2 pt-2 pb-3 space-y-0.5"
      style="border-top: 1px solid var(--sidebar-divider-color)"
    >
      <!-- Status row -->
      <div v-if="!isCollapsed" class="flex items-center gap-1.5 px-2 py-1.5">
        <span
          v-if="authStore.activeOrganization"
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border"
          :class="authStore.isAdmin
            ? 'bg-[oklch(0.55_0.20_258/0.10)] text-primary border-[oklch(0.55_0.20_258/0.20)]'
            : 'bg-[var(--glass-bg-subtle)] text-muted-fg border-[var(--glass-border)]'"
        >
          <Shield :size="9" />
          {{ authStore.isAdmin ? 'Admin' : 'Member' }}
        </span>
        <span
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border"
          :class="isConnected
            ? 'bg-[oklch(0.52_0.16_150/0.10)] text-success border-[oklch(0.52_0.16_150/0.20)]'
            : 'bg-[oklch(0.55_0.22_27/0.10)] text-error border-[oklch(0.55_0.22_27/0.20)]'"
          :title="isConnected ? 'Real-time connected' : 'Real-time disconnected'"
        >
          <component :is="isConnected ? Wifi : WifiOff" :size="9" />
          {{ isConnected ? 'Live' : 'Offline' }}
        </span>
      </div>
      <!-- Collapsed status icons -->
      <div v-else class="flex flex-col items-center gap-1 py-1">
        <div
          v-if="authStore.activeOrganization && authStore.isAdmin"
          class="w-7 h-7 rounded-md flex items-center justify-center"
          style="background: oklch(0.55 0.20 258 / 0.10); border: 1px solid oklch(0.55 0.20 258 / 0.20)"
          title="Admin"
        >
          <Shield :size="12" class="text-primary" />
        </div>
        <div
          class="w-7 h-7 rounded-md border flex items-center justify-center"
          :style="isConnected
            ? 'background: oklch(0.52 0.16 150 / 0.10); border-color: oklch(0.52 0.16 150 / 0.20); color: var(--color-success)'
            : 'background: oklch(0.55 0.22 27 / 0.10); border-color: oklch(0.55 0.22 27 / 0.20); color: var(--color-error)'"
          :title="isConnected ? 'Real-time connected' : 'Real-time disconnected'"
        >
          <component :is="isConnected ? Wifi : WifiOff" :size="12" />
        </div>
      </div>

      <!-- Theme toggle -->
      <button
        class="flex items-center gap-2.5 w-full text-xs text-muted-fg hover:text-fg cursor-pointer transition-[background,color] duration-[200ms]"
        :class="isCollapsed ? 'px-2 py-2 justify-center' : 'px-2.5 py-2'"
        style="border-radius: var(--radius-sm); [transition-timing-function:var(--spring-snappy)]"
        @click="cycleTheme"
        :title="isCollapsed ? `${themeLabel} theme` : ''"
      >
        <component :is="themeIcon" :size="13" class="shrink-0" />
        <span v-if="!isCollapsed" style="letter-spacing: var(--tracking-body)">
          {{ themeLabel }} theme
        </span>
      </button>

      <!-- User row -->
      <div
        class="flex items-center gap-2 px-2 py-1.5 rounded-[var(--radius-sm)]"
        :class="isCollapsed ? 'justify-center' : ''"
      >
        <!-- Avatar -->
        <div
          class="w-6 h-6 rounded-full text-primary text-[10px] font-bold flex items-center justify-center shrink-0"
          style="background: oklch(0.55 0.20 258 / 0.15); color: var(--color-primary)"
        >
          {{ userInitials }}
        </div>
        <div v-if="!isCollapsed" class="flex-1 min-w-0">
          <p
            class="text-xs text-fg font-medium truncate leading-tight"
            :title="authStore.user?.email"
            style="letter-spacing: var(--tracking-body)"
          >
            {{ authStore.user?.email ?? '—' }}
          </p>
        </div>
        <button
          class="text-muted-fg hover:text-error transition-[color] duration-[200ms] cursor-pointer shrink-0"
          title="Sign out"
          @click="handleLogout"
        >
          <LogOut :size="13" />
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
/* Nav link hover — subtle glass highlight */
a:not(.router-link-active):hover {
  background: var(--glass-bg-subtle) !important;
  color: var(--color-foreground) !important;
}
</style>
