<script setup lang="ts">
import { useTheme } from '@/composables/useTheme'
import { Toaster } from '@/components/ui/sonner'

useTheme()
</script>

<template>
  <RouterView />
  <Toaster rich-colors close-button />
</template>
