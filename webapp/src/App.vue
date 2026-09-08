<script setup lang="ts">
import { useTheme } from '@/composables/useTheme'
import { Toaster } from '@/components/ui/sonner'

const { isDark } = useTheme()
</script>

<template>
  <RouterView />
  <Toaster :theme="isDark ? 'dark' : 'light'" close-button />
</template>
