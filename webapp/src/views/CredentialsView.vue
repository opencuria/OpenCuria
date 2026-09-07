<script setup lang="ts">
// Übergangs-Wrapper (Schritt 5): Kern lebt in CredentialsPanel,
// diese Route bleibt bis Schritt 6 bestehen.
import CredentialsPanel from '@/components/settings/CredentialsPanel.vue'
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-foreground">Credentials</h2>
        <p class="text-sm text-muted-foreground mt-1">
          Manage credentials injected into workspaces. Personal credentials are yours across all
          organizations; organization credentials are shared with all members.
        </p>
      </div>
    </div>

    <CredentialsPanel />
  </div>
</template>
