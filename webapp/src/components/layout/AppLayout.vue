<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from './AppSidebar.vue'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
import { SidebarInset, SidebarProvider, SidebarTrigger } from '@/components/ui/sidebar'
import { Separator } from '@/components/ui/separator'

const route = useRoute()
const showMobileTopBar = computed(() => !route.meta.hideTopBar)
</script>

<template>
  <SidebarProvider>
    <AppSidebar />
    <SidebarInset>
      <header
        v-if="showMobileTopBar"
        class="flex h-14 shrink-0 items-center gap-2 border-b px-4 lg:hidden"
      >
        <SidebarTrigger />
        <Separator orientation="vertical" class="mr-2 h-4" />
        <OpenCuriaLogo icon-only alt="OpenCuria" class="size-8" />
        <span class="text-sm font-semibold">OpenCuria</span>
      </header>

      <main class="flex-1 overflow-y-auto overflow-x-hidden p-4 lg:p-6">
        <RouterView />
      </main>
    </SidebarInset>
  </SidebarProvider>
</template>
