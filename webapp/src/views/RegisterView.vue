<script setup lang="ts">
import { RouterLink } from 'vue-router'
import OpenCuriaLogo from '@/components/branding/OpenCuriaLogo.vue'
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-bg px-4">
    <div
      class="w-full max-w-md bg-surface rounded-[var(--radius-lg)] shadow-lg p-8 border border-border"
    >
      <!-- Brand -->
      <div class="flex items-center gap-3 mb-8 justify-center">
        <OpenCuriaLogo class="h-10 w-auto" alt="OpenCuria" />
      </div>

      <h2 class="text-lg font-semibold text-fg mb-4 text-center">Registration closed</h2>

      <p class="text-sm text-muted-fg text-center mb-6">
        OpenCuria is currently invite-only. New registrations are not accepted at this time.
      </p>

      <p class="text-center text-sm text-muted-fg">
        Already have an account?
        <RouterLink to="/login" class="text-primary hover:text-primary-hover font-medium">
          Sign in
        </RouterLink>
      </p>
    </div>
  </div>
</template>
