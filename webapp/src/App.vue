<script setup lang="ts">
import { useTheme } from '@/composables/useTheme'
import { Toaster } from '@/components/ui/sonner'
import ClipboardDialog from '@/components/common/ClipboardDialog.vue'

const { isDark } = useTheme()
</script>

<template>
  <RouterView />
  <ClipboardDialog />
  <Toaster :theme="isDark ? 'dark' : 'light'" close-button />
</template>
