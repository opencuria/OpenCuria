<script setup lang="ts">
// Schlanker Legacy-Wrapper (Schritt 5/6):
// Die Route `/org-settings` ist im Router ein Redirect auf `/?settings=<tab>`
// (siehe router/index.ts) und rendert diese View regulär gar nicht.
// Falls sie doch je direkt gemountet wird (z. B. Tests), defensiv auf "/"
// mit gemapptem `?settings=`-Tab weiterleiten und Sheet-Event feuern.
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  OPEN_SETTINGS_EVENT,
  SETTINGS_QUERY_PARAM,
  resolveSettingsTab,
} from '@/components/settings/settingsTabs'

const route = useRoute()
const router = useRouter()

onMounted(() => {
  const tab = resolveSettingsTab(route.query.tab)
  window.dispatchEvent(new CustomEvent(OPEN_SETTINGS_EVENT, { detail: { tab } }))
  void router.replace({ path: '/', query: { [SETTINGS_QUERY_PARAM]: tab } })
})
</script>

<template>
  <div class="p-4 text-sm text-muted-foreground">Weiterleitung zu den Einstellungen …</div>
</template>
