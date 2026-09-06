<script setup lang="ts">
// Übergangs-Wrapper (Schritt 5): Kern lebt in CapturedImagesPanel,
// diese Route bleibt bis Schritt 6 bestehen.
import CapturedImagesPanel from '@/components/settings/CapturedImagesPanel.vue'
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold text-foreground">Captured Images</h2>
        <p class="text-sm text-muted-foreground mt-1">
          Reusable workspace images captured from your workspaces.
        </p>
      </div>
    </div>

    <CapturedImagesPanel />
  </div>
</template>
