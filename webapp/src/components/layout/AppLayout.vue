<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from './AppSidebar.vue'
import ToastContainer from '@/components/toast/ToastContainer.vue'
import { Menu } from 'lucide-vue-next'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'

const mobileSidebarOpen = ref(false)
const route = useRoute()
const showMobileTopBar = computed(() => !route.meta.hideTopBar)
</script>

<template>
  <div class="flex h-screen overflow-hidden text-fg" style="background-color: var(--color-background)">
    <!-- Mobile sidebar overlay — frosted glass backdrop -->
    <div
      v-if="mobileSidebarOpen"
      class="fixed inset-0 z-30 lg:hidden"
      style="
        background: oklch(0.10 0.02 248 / 0.35);
        backdrop-filter: blur(6px) saturate(1.1);
        -webkit-backdrop-filter: blur(6px) saturate(1.1);
      "
      @click="mobileSidebarOpen = false"
    />

    <!-- Sidebar -->
    <AppSidebar :mobile-open="mobileSidebarOpen" @close="mobileSidebarOpen = false" />

    <!-- Main content area -->
    <div class="flex flex-1 flex-col overflow-hidden min-w-0">
      <!-- Mobile-only top bar -->
      <div
        v-if="showMobileTopBar"
        class="flex lg:hidden items-center gap-3 px-4 py-3 shrink-0"
        style="
          background: var(--glass-bg);
          backdrop-filter: var(--glass-filter);
          -webkit-backdrop-filter: var(--glass-filter);
          border-bottom: 1px solid var(--glass-border);
          box-shadow: var(--glass-shadow-sm);
        "
      >
        <button
          class="flex items-center justify-center w-8 h-8 text-muted-fg hover:text-fg transition-[color,background] duration-[200ms] cursor-pointer"
          style="
            border-radius: var(--radius-sm);
            background: var(--glass-bg-subtle);
            backdrop-filter: var(--glass-filter-subtle);
            border: 1px solid var(--glass-border);
          "
          title="Open menu"
          @click="mobileSidebarOpen = true"
        >
          <Menu :size="18" />
        </button>
        <OpenCuriaLogo icon-only alt="OpenCuria" class="h-8 w-8" />
        <span
          class="text-sm font-semibold text-fg"
          style="letter-spacing: var(--tracking-body)"
        >OpenCuria</span>
      </div>

      <main class="flex-1 overflow-y-auto overflow-x-hidden p-[var(--sp-4)] lg:p-[var(--sp-5)]">
        <RouterView />
      </main>
    </div>

    <!-- Global toast notifications -->
    <ToastContainer />
  </div>
</template>
