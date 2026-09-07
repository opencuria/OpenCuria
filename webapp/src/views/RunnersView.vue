<script setup lang="ts">
// Übergangs-Wrapper (Schritt 5): Kern lebt in RunnersPanel,
// diese Route bleibt bis Schritt 6 bestehen.
import RunnersPanel from '@/components/settings/RunnersPanel.vue'
</script>

<template>
  <div class="space-y-6">
    <!-- Page header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-foreground">Runners</h2>
        <p class="text-sm text-muted-foreground mt-1">
          Manage runner instances that execute AI coding agents.
        </p>
      </div>
    </div>

    <RunnersPanel />
  </div>
</template>
