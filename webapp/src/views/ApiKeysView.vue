<script setup lang="ts">
// Übergangs-Wrapper (Schritt 5): Kern lebt in ApiKeysPanel,
// diese Route bleibt bis Schritt 6 bestehen.
import ApiKeysPanel from '@/components/settings/ApiKeysPanel.vue'
</script>

<template>
  <div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-foreground">API Keys</h2>
        <p class="text-sm text-muted-foreground mt-1">
          Long-lived keys for external integrations. Use
          <span class="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">Authorization: Bearer kai_…</span>
          or
          <span class="font-mono text-xs bg-muted px-1.5 py-0.5 rounded">X-API-Key: kai_…</span>.
        </p>
      </div>
    </div>

    <ApiKeysPanel />
  </div>
</template>
