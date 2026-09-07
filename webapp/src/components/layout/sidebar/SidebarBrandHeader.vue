<script setup lang="ts">
/**
 * Logo + organization switcher. Expanded sidebars show the OpenCuria brand;
 * multiple organizations can be switched from the dropdown.
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { Check, ChevronsUpDown, Plus } from '@lucide/vue'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from '@/components/ui/sidebar'
import { useAuthStore } from '@/stores/auth'

const emit = defineEmits<{
  home: []
  'switch-organization': [orgId: string]
}>()

const authStore = useAuthStore()
const { isMobile, state, setOpen } = useSidebar()
const showSwitcher = computed(() => authStore.organizations.length > 1)
const isCollapsed = computed(() => !isMobile.value && state.value === 'collapsed')

function expandSidebar(): void {
  if (isCollapsed.value) setOpen(true)
}
</script>

<template>
  <SidebarMenu>
    <SidebarMenuItem>
      <DropdownMenu v-if="showSwitcher && !isCollapsed">
        <DropdownMenuTrigger as-child>
          <SidebarMenuButton size="lg" tooltip="Organisation wechseln">
            <OpenCuriaLogo icon-only alt="OpenCuria" class="size-8!" @click="expandSidebar" />
            <span class="truncate font-semibold">OpenCuria</span>
            <ChevronsUpDown class="ml-auto size-4 shrink-0" />
          </SidebarMenuButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent class="min-w-56" align="start">
          <DropdownMenuItem
            v-for="org in authStore.organizations"
            :key="org.id"
            @click="emit('switch-organization', org.id)"
          >
            <Avatar class="size-5 rounded-md">
              <AvatarFallback class="rounded-md text-[10px]">
                {{ org.name.charAt(0).toUpperCase() }}
              </AvatarFallback>
            </Avatar>
            <span class="truncate">{{ org.name }}</span>
            <Check v-if="org.id === authStore.activeOrganizationId" class="ml-auto size-4" />
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem as-child>
            <RouterLink to="/create-organization" class="flex items-center gap-2">
              <Plus class="size-4" />
              Neue Organisation
            </RouterLink>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <SidebarMenuButton
        v-else-if="isCollapsed"
        size="lg"
        tooltip="OpenCuria"
        @click="expandSidebar"
      >
        <OpenCuriaLogo icon-only alt="OpenCuria" class="size-8!" @click="expandSidebar" />
        <span class="truncate font-semibold">OpenCuria</span>
      </SidebarMenuButton>

      <SidebarMenuButton v-else size="lg" as-child tooltip="OpenCuria">
        <RouterLink to="/" @click="emit('home')">
          <OpenCuriaLogo icon-only alt="OpenCuria" class="size-8!" />
          <span class="truncate font-semibold">OpenCuria</span>
        </RouterLink>
      </SidebarMenuButton>
    </SidebarMenuItem>
  </SidebarMenu>
</template>
