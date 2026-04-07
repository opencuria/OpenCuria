<script setup lang="ts">
import { useTheme } from '@/composables/useTheme'

// Initialize theme system (reads from localStorage / system preference)
useTheme()
</script>

<template>
  <RouterView />
</template>
