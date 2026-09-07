<script setup lang="ts">
/**
 * Account row with a live/offline dot on the avatar and the user menu.
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { BookOpen, Check, LogOut, Monitor, Moon, Settings, Sun } from '@lucide/vue'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { SidebarMenu, SidebarMenuButton, SidebarMenuItem } from '@/components/ui/sidebar'
import { useTheme } from '@/composables/useTheme'
import { useAuthStore } from '@/stores/auth'
import { isConnected } from '@/services/socket'

const emit = defineEmits<{
  settings: []
  logout: []
  navigate: []
}>()

const authStore = useAuthStore()
const { mode, setTheme } = useTheme()

const userInitials = computed(() => {
  const email = authStore.user?.email ?? ''
  return email.charAt(0).toUpperCase() || '?'
})
</script>

<template>
  <SidebarMenu>
    <SidebarMenuItem>
      <DropdownMenu>
        <DropdownMenuTrigger as-child>
          <SidebarMenuButton class="h-10" :tooltip="authStore.user?.email ?? 'Konto'">
            <span class="relative shrink-0">
              <Avatar class="size-6">
                <AvatarFallback class="text-[10px]">{{ userInitials }}</AvatarFallback>
              </Avatar>
              <span
                data-testid="live-dot"
                class="absolute -right-0.5 -bottom-0.5 size-2 rounded-full ring-2 ring-sidebar"
                :class="isConnected ? 'bg-green-500' : 'bg-muted-foreground/50'"
                :aria-label="isConnected ? 'Live' : 'Offline'"
                :title="isConnected ? 'Live' : 'Offline'"
              />
            </span>
            <span class="truncate text-xs">{{ authStore.user?.email ?? '—' }}</span>
          </SidebarMenuButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent class="w-60 text-xs" align="end" side="top">
          <DropdownMenuItem @click="emit('settings')">
            <Settings class="size-4" />
            Einstellungen öffnen
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem @click="setTheme('light')">
            <Sun class="size-4" />
            <span class="flex-1">Hell</span>
            <Check v-if="mode === 'light'" class="size-4" />
          </DropdownMenuItem>
          <DropdownMenuItem @click="setTheme('dark')">
            <Moon class="size-4" />
            <span class="flex-1">Dunkel</span>
            <Check v-if="mode === 'dark'" class="size-4" />
          </DropdownMenuItem>
          <DropdownMenuItem @click="setTheme('auto')">
            <Monitor class="size-4" />
            <span class="flex-1">Auto</span>
            <Check v-if="mode === 'auto'" class="size-4" />
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem as-child>
            <RouterLink to="/docs" class="flex items-center gap-2" @click="emit('navigate')">
              <BookOpen class="size-4" />
              Docs
            </RouterLink>
          </DropdownMenuItem>
          <DropdownMenuItem @click="emit('logout')">
            <LogOut class="size-4" />
            Abmelden
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </SidebarMenuItem>
  </SidebarMenu>
</template>
