<script setup lang="ts">
// Übergangs-Wrapper (Schritt 5): Kern lebt in SkillsPanel,
// diese Route bleibt bis Schritt 6 bestehen.
import SkillsPanel from '@/components/settings/SkillsPanel.vue'
</script>

<template>
  <div class="space-y-6">
    <!-- Page header -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold text-foreground">Skills</h2>
        <p class="text-sm text-muted-foreground mt-1">
          Reusable prompt fragments appended to harness prompts. Personal skills are yours across all
          organizations; organization skills are shared with all members.
        </p>
      </div>
    </div>

    <SkillsPanel />
  </div>
</template>
