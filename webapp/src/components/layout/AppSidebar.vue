<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
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
  ChevronDown,
  Wifi,
  WifiOff,
  Shield,
  Key,
  Camera,
  Settings2,
} from '@lucide/vue'
import { useTheme } from '@/composables/useTheme'
import { useAuthStore } from '@/stores/auth'
import { connect as connectSocket, disconnect as disconnectSocket, isConnected } from '@/services/socket'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarSeparator,
  useSidebar,
} from '@/components/ui/sidebar'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'

const route = useRoute()
const router = useRouter()
const { mode, setTheme } = useTheme()
const authStore = useAuthStore()
const { isMobile, setOpenMobile } = useSidebar()

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

function toggleSection(id: string, open: boolean): void {
  sectionState.value[id] = open
  localStorage.setItem(SECTION_KEY, JSON.stringify(sectionState.value))
}

const isAdmin = computed(() => authStore.isAdmin)

const navSections = computed(() => [
  {
    id: 'main',
    label: null,
    items: [{ to: '/', label: 'Dashboard', icon: LayoutDashboard }],
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
    items: [{ to: '/docs', label: 'Docs', icon: BookOpen }],
  },
])

function isActive(path: string): boolean {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

function closeMobileSidebar(): void {
  if (isMobile.value) setOpenMobile(false)
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
  <Sidebar collapsible="icon">
    <SidebarHeader>
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton size="lg" as-child>
            <RouterLink to="/" @click="closeMobileSidebar">
              <OpenCuriaLogo icon-only alt="OpenCuria" class="size-8" />
              <span class="font-semibold">OpenCuria</span>
            </RouterLink>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>

      <SidebarMenu v-if="authStore.organizations.length > 0">
        <SidebarMenuItem>
          <DropdownMenu>
            <DropdownMenuTrigger as-child>
              <SidebarMenuButton class="w-full">
                <Avatar class="size-5 rounded-md">
                  <AvatarFallback class="rounded-md text-[10px]">
                    {{ authStore.activeOrganization?.name?.charAt(0)?.toUpperCase() ?? '?' }}
                  </AvatarFallback>
                </Avatar>
                <span class="truncate">
                  {{ authStore.activeOrganization?.name ?? 'Select organization' }}
                </span>
                <ChevronsUpDown class="ml-auto size-4" />
              </SidebarMenuButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent class="w-[--reka-dropdown-menu-trigger-width] min-w-56" align="start">
              <DropdownMenuItem
                v-for="org in authStore.organizations"
                :key="org.id"
                @click="switchOrganization(org.id)"
              >
                <span class="truncate">{{ org.name }}</span>
                <Check v-if="org.id === authStore.activeOrganizationId" class="ml-auto size-4" />
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem as-child>
                <RouterLink to="/create-organization" class="flex items-center gap-2">
                  <Plus class="size-4" />
                  New organization
                </RouterLink>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarHeader>

    <SidebarContent>
      <template v-for="section in navSections" :key="section.id">
        <SidebarSeparator v-if="section.id !== 'main'" />

        <SidebarGroup v-if="section.label">
          <Collapsible
            :open="isSectionOpen(section.id)"
            class="group/collapsible"
            @update:open="(open) => toggleSection(section.id, open)"
          >
            <SidebarGroupLabel as-child>
              <CollapsibleTrigger class="flex w-full items-center">
                {{ section.label }}
                <ChevronDown class="ml-auto size-4 transition-transform group-data-[state=open]/collapsible:rotate-180" />
              </CollapsibleTrigger>
            </SidebarGroupLabel>
            <CollapsibleContent>
              <SidebarGroupContent>
                <SidebarMenu>
                  <SidebarMenuItem v-for="item in section.items" :key="item.to">
                    <SidebarMenuButton as-child :is-active="isActive(item.to)" :tooltip="item.label">
                      <RouterLink :to="item.to" @click="closeMobileSidebar">
                        <component :is="item.icon" />
                        <span>{{ item.label }}</span>
                      </RouterLink>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                </SidebarMenu>
              </SidebarGroupContent>
            </CollapsibleContent>
          </Collapsible>
        </SidebarGroup>

        <SidebarGroup v-else>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem v-for="item in section.items" :key="item.to">
                <SidebarMenuButton as-child :is-active="isActive(item.to)" :tooltip="item.label">
                  <RouterLink :to="item.to" @click="closeMobileSidebar">
                    <component :is="item.icon" />
                    <span>{{ item.label }}</span>
                  </RouterLink>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </template>
    </SidebarContent>

    <SidebarFooter>
      <div class="flex flex-wrap gap-1 px-2 group-data-[collapsible=icon]:hidden">
        <Badge v-if="authStore.activeOrganization" :variant="authStore.isAdmin ? 'default' : 'secondary'">
          <Shield class="size-3" />
          {{ authStore.isAdmin ? 'Admin' : 'Member' }}
        </Badge>
        <Badge :variant="isConnected ? 'secondary' : 'destructive'">
          <component :is="isConnected ? Wifi : WifiOff" class="size-3" />
          {{ isConnected ? 'Live' : 'Offline' }}
        </Badge>
      </div>

      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton :tooltip="`${themeLabel} theme`" @click="cycleTheme">
            <component :is="themeIcon" />
            <span>{{ themeLabel }} theme</span>
          </SidebarMenuButton>
        </SidebarMenuItem>
        <SidebarMenuItem>
          <SidebarMenuButton class="h-12" :tooltip="authStore.user?.email ?? 'Account'">
            <Avatar class="size-6">
              <AvatarFallback class="text-[10px]">{{ userInitials }}</AvatarFallback>
            </Avatar>
            <div class="grid flex-1 text-left text-xs leading-tight">
              <span class="truncate font-medium">{{ authStore.user?.email ?? '—' }}</span>
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              class="ml-auto"
              title="Sign out"
              @click.stop="handleLogout"
            >
              <LogOut />
            </Button>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarFooter>

    <SidebarRail />
  </Sidebar>
</template>
